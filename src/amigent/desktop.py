"""Put the viewer into the Windows Start menu and on the desktop.

Works from WSL and from a Windows Python. The shortcut starts the viewer
without a console window; it ends again when its window is closed.
"""

from __future__ import annotations

import getpass
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

from .viewer import windows_exe

NAME = "Amiga Remote"
ICON = Path(__file__).with_name("web") / "amigent.ico"
SETTINGS = ("AMIGENT_CTL_PORT", "AMIGENT_SCREEN_PORT", "AMIGENT_VIEWER_PORT", "AMIGENT_FLUSH_VOLUMES")


def _wsl() -> bool:
    return "microsoft" in platform.release().lower()


def _powershell(script: str) -> str:
    exe = windows_exe("powershell.exe") if _wsl() else "powershell"
    cwd = "/mnt/c" if _wsl() and os.path.isdir("/mnt/c") else None  # no UNC working directory
    result = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, cwd=cwd, check=True)
    return result.stdout.strip()


def _local(windows_path: str) -> Path:
    """Where a Windows path can be written from here."""
    if not _wsl():
        return Path(windows_path)
    return Path(subprocess.run(["wslpath", "-u", windows_path], capture_output=True, text=True,
                               check=True).stdout.strip())


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg or not arg else arg


def _launcher(host: str) -> tuple[str, str]:
    """File name and text of the script the shortcut runs."""
    settings = {k: os.environ[k] for k in SETTINGS if os.environ.get(k)}
    package_root = str(Path(__file__).resolve().parents[1])
    args = [host, "--exit-when-closed"]
    if _wsl():
        command = ["wsl.exe", "-d", os.environ.get("WSL_DISTRO_NAME", "Ubuntu"), "-u", getpass.getuser(),
                   "--exec", "/usr/bin/env", f"PYTHONPATH={package_root}",
                   *(f"{k}={v}" for k, v in settings.items()),
                   sys.executable, "-m", "amigent.viewer", *args]
        line = " ".join(_quote(a) for a in command).replace('"', '""')
        return f"{NAME}.vbs", f'CreateObject("WScript.Shell").Run "{line}", 0, False\r\n'
    lines = ["import os, sys", f"sys.path.insert(0, {package_root!r})"]
    lines += [f"os.environ[{k!r}] = {v!r}" for k, v in settings.items()]
    lines += [f"sys.argv = ['amigent-viewer', *{args!r}]", "from amigent.viewer import main", "main()"]
    return f"{NAME}.pyw", "\n".join(lines) + "\n"


def install(host: str) -> list[str]:
    """Create the launcher and both shortcuts; returns the shortcut paths."""
    if not (_wsl() or sys.platform == "win32"):
        raise SystemExit("shortcuts are for Windows; elsewhere run amigent-viewer directly")
    folder = PureWindowsPath(_powershell("[Environment]::GetFolderPath('LocalApplicationData')")) / "amigent"
    local = _local(str(folder))
    local.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON, local / "amigent.ico")
    name, text = _launcher(host)
    (local / name).write_text(text, encoding="utf-8")
    launcher = folder / name
    if _wsl():
        target, arguments = r"$env:WINDIR\System32\wscript.exe", f'"{launcher}"'
    else:
        target, arguments = str(Path(sys.executable).with_name("pythonw.exe")), f'"{launcher}"'
    script = f"""
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {{
  $link = $shell.CreateShortcut((Join-Path $dir '{NAME}.lnk'))
  $link.TargetPath = "{target}"
  $link.Arguments = '{arguments}'
  $link.WorkingDirectory = '{folder}'
  $link.IconLocation = '{folder / "amigent.ico"},0'
  $link.Description = 'Watch and drive the Amiga'
  $link.Save()
  $link.FullName
}}
"""
    return _powershell(script).splitlines()


def uninstall() -> list[str]:
    """Remove both shortcuts and the launcher folder; returns what was removed."""
    script = f"""
foreach ($dir in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {{
  $path = Join-Path $dir '{NAME}.lnk'
  if (Test-Path $path) {{ Remove-Item $path; $path }}
}}
$folder = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'amigent'
if (Test-Path $folder) {{ Remove-Item $folder -Recurse; $folder }}
"""
    return _powershell(script).splitlines()
