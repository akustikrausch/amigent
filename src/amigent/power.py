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

from . import net, safety, tls
from .errors import AmigaError
from .roster import address

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


def greet(host: str, port: int, timeout: float, connect_timeout: float,
          handshake: float | None = None) -> bytes:
    """The service's hello: on its TLS port under AMIGENT_REQUIRE_TLS, else on port itself."""
    if safety.require_tls():
        return tls.greet(host, port, timeout, connect_timeout, handshake)
    return net.greet(address(host), port, timeout, connect_timeout=connect_timeout)


def wait_for_port(host: str, port: int, wait: float) -> bool:
    """True once the service says hello; polls every 10 s until wait runs out.

    Under AMIGENT_REQUIRE_TLS the hello comes after the handshake, which gets
    tls.HANDSHAKE_WAIT: a slow one is an Amiga that came back, not one still away."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if greet(host, port, 8, connect_timeout=5, handshake=tls.HANDSHAKE_WAIT):
            return True
        time.sleep(10)
    return False


def _tls_port_open(host: str, port: int, timeout: float) -> bool:
    """Whether amigentd's TLS port still takes a connection. Nothing is sent: a slow
    handshake (a 68000, a plipbox) must not read as an Amiga that went away."""
    try:
        with socket.create_connection((address(host), port + 1), timeout):
            return True
    except OSError:
        return False


def wait_gone(host: str, port: int, wait: float = 30) -> bool:
    """True once the service stops saying hello: proof the restart really happened."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if safety.require_tls():
            if not _tls_port_open(host, port, 3):
                return True
        elif not greet(host, port, 3, connect_timeout=3):
            return True
        time.sleep(1)
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


def flush_volumes() -> list[str]:
    """The volumes to commit before any restart: AMIGENT_FLUSH_VOLUMES, else SYS:."""
    return os.environ.get("AMIGENT_FLUSH_VOLUMES", "SYS:").split()


def reboot(commands, volumes) -> None:
    """Flush the disks, then pull the reset: the clean way down, not the plug."""
    try:
        commands.stat("C:Reboot")
    except AmigaError as e:
        if e.code is None:
            raise
        raise AmigaError("this Amiga has no C:Reboot (Workbench before 3.0 lacks it): restart it by "
                         "hand, or copy C:Reboot over from a newer Workbench") from e
    commands.flush(volumes)
    try:
        commands.exec("Run >NIL: C:Reboot", timeout=5)
    except (OSError, AmigaError):
        pass  # it dies mid-sentence, that's the whole point
    commands.close()
