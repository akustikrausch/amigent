"""Optional mains power control: the big red switch, via a command you supply.

AMIGENT_POWER_CMD names a program that takes 'on' or 'off', say a script for
your smart plug. Without it the amiga_power tool reports an error.
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import time

from .errors import AmigaError

OFF_SECONDS = 30
BOOT_WAIT = 300
RESTART_WAIT = 120  # grace after a restart before we reach for the plug, if we have one


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
    """True once the service says hello; polls every 10 s until wait runs out."""
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
    """Off, wait, on, until the service answers; returns the attempts it took."""
    for attempt in range(1, attempts + 1):
        set_power(False)
        time.sleep(OFF_SECONDS)
        set_power(True)
        if wait_for_port(host, port, BOOT_WAIT):
            return attempt
    raise PowerError(f"no answer from {host}:{port} after {attempts} power cycles")


def reboot(commands, volumes) -> None:
    """Flush the disks, then pull the reset: the clean way down, not the plug."""
    commands.flush(volumes)
    try:
        commands.exec("Run >NIL: C:Reboot", timeout=5)
    except (OSError, AmigaError):
        pass  # it dies mid-sentence, that's the whole point
    commands.close()
