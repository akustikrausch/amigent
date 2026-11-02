"""Optional mains power control through a user-supplied command.

AMIGENT_POWER_CMD names a program that accepts 'on' or 'off', for example a
script for your smart plug. Without it the amiga_power tool reports an error.
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import time

OFF_SECONDS = 30
BOOT_WAIT = 300
RESTART_WAIT = 120  # after a restart: how long to wait before cutting the power, if we can


class PowerError(Exception):
    pass


def available() -> bool:
    return bool(os.environ.get("AMIGENT_POWER_CMD"))


def set_power(on: bool) -> None:
    command = os.environ.get("AMIGENT_POWER_CMD")
    if not command:
        raise PowerError("no power control configured; set AMIGENT_POWER_CMD")
    subprocess.run([*shlex.split(command), "on" if on else "off"], check=True, timeout=60)


def wait_for_port(host: str, port: int, wait: float) -> bool:
    """True once the service sends its greeting."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            from .machines import address
            with socket.create_connection((address(host), port), 5) as s:
                s.settimeout(8)
                if s.recv(8):
                    return True
        except OSError:
            pass
        time.sleep(10)
    return False


def power_cycle(host: str, port: int, attempts: int = 3) -> int:
    """Cycle the power until the service answers; returns the attempts used."""
    for attempt in range(1, attempts + 1):
        set_power(False)
        time.sleep(OFF_SECONDS)
        set_power(True)
        if wait_for_port(host, port, BOOT_WAIT):
            return attempt
    raise PowerError(f"no answer from {host}:{port} after {attempts} power cycles")
