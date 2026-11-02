"""Hand amigentd to the Amiga over the network, for the very first step.

`amigent-setup --serve` starts a small web server on this PC with amigentd and
amigentd.000. On the Amiga, open the address it prints in a browser, or fetch
the file in a Shell. Under WSL the server runs in Windows Python, because the
local network does not reach servers inside WSL.
"""

from __future__ import annotations

import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from .setup import AMIGA
from .viewer import windows_exe

AMIGA_BIN = AMIGA / "bin"

FILES = ("amigentd", "amigentd.000")


def lan_address() -> str:
    """The address other machines on the network reach this PC at."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # nothing is sent; this only picks the outgoing interface
        return s.getsockname()[0]


def in_wsl() -> bool:
    return "microsoft" in platform.release().lower()


def _windows_folder() -> tuple[Path, str]:
    """A fresh folder in Windows' temp directory, as a WSL path and as a Windows path."""
    cmd = windows_exe("cmd.exe")
    temp = subprocess.run([cmd, "/c", "echo %TEMP%"], capture_output=True, text=True,
                          cwd="/mnt/c").stdout.strip()
    base = Path(subprocess.run(["wslpath", "-u", temp], capture_output=True, text=True).stdout.strip())
    folder = base / "amigent-serve"
    folder.mkdir(parents=True, exist_ok=True)
    return folder, subprocess.run(["wslpath", "-w", str(folder)], capture_output=True,
                                  text=True).stdout.strip()


def server_command(port: int) -> tuple[list[str], Path]:
    """The http.server command line and the folder it serves, filled with the files."""
    if in_wsl():
        python = next((p for p in (windows_exe("py.exe"), windows_exe("python.exe"))
                       if shutil.which(p) or Path(p).exists()), "")
        if not python:
            raise SystemExit("No Windows Python found. Install it from python.org, or copy "
                             "amigentd to the Amiga another way.")
        folder, shown = _windows_folder()
        args = ["-3"] if python.lower().endswith("py.exe") else []
        command = [python, *args, "-m", "http.server", str(port), "--bind", "0.0.0.0",
                   "--directory", shown]
    else:
        folder = Path(tempfile.mkdtemp(prefix="amigent-serve-"))
        command = [sys.executable, "-m", "http.server", str(port), "--bind", "0.0.0.0",
                   "--directory", str(folder)]
    for name in FILES:
        if (AMIGA_BIN / name).exists():
            shutil.copyfile(AMIGA_BIN / name, folder / name)
    return command, folder


def serve(port: int = 8001, minutes: int = 30) -> int:
    command, _ = server_command(port)
    url = f"http://{lan_address()}:{port}/"
    print(f"""Serving amigentd at {url} for {minutes} minutes (Ctrl-C ends it sooner).

On the Amiga, open {url} in a browser and save amigentd (amigentd.000 on a
plain 68000) to RAM:. Or, if it has wget, in a Shell:

    wget -O RAM:amigentd {url}amigentd
    Protect RAM:amigentd +e
    RAM:amigentd SETUP

Then run amigent-setup here. If nothing arrives, the PC's firewall may ask once
whether Python may accept connections on the private network.""", flush=True)
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        process.wait(minutes * 60)
    except subprocess.TimeoutExpired:
        pass
    except KeyboardInterrupt:
        print()
    finally:
        process.terminate()
    return 0
