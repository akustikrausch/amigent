"""Install the viewer as a Windows program: Start menu, desktop, Installed apps.

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

from . import __version__
from .viewer import windows_exe

NAME = "Amigent"
OLD_NAMES = ("Amiga Remote",)  # shortcuts of earlier versions, removed on install
PUBLISHER = "Akustikrausch"
HOMEPAGE = "https://github.com/akustikrausch/amigent"
REGISTRY = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\amigent"
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


def _script(name: str, args: list[str]) -> tuple[str, str]:
    """File name and text of a script that runs amigent-viewer with args, without a console."""
    settings = {k: os.environ[k] for k in SETTINGS if os.environ.get(k)}
    package_root = str(Path(__file__).resolve().parents[1])
    if _wsl():
        command = ["wsl.exe", "-d", os.environ.get("WSL_DISTRO_NAME", "Ubuntu"), "-u", getpass.getuser(),
                   "--exec", "/usr/bin/env", f"PYTHONPATH={package_root}",
                   *(f"{k}={v}" for k, v in settings.items()),
                   sys.executable, "-m", "amigent.viewer", *args]
        line = " ".join(_quote(a) for a in command).replace('"', '""')
        return f"{name}.vbs", f'CreateObject("WScript.Shell").Run "{line}", 0, False\r\n'
    lines = ["import os, sys", f"sys.path.insert(0, {package_root!r})"]
    lines += [f"os.environ[{k!r}] = {v!r}" for k, v in settings.items()]
    lines += [f"sys.argv = ['amigent-viewer', *{args!r}]", "from amigent.viewer import main", "main()"]
    return f"{name}.pyw", "\n".join(lines) + "\n"


def _launcher(host: str) -> tuple[str, str]:
    return _script(NAME, [host, "--exit-when-closed"])


def _runner() -> str:
    """The program that runs our scripts without a console window."""
    if _wsl():
        return r"$env:WINDIR\System32\wscript.exe"
    return str(Path(sys.executable).with_name("pythonw.exe"))


def install(host: str) -> list[str]:
    """Create launcher, shortcuts and the Installed apps entry; returns what was created."""
    if not (_wsl() or sys.platform == "win32"):
        raise SystemExit("this is for Windows; elsewhere run amigent-viewer directly")
    folder = PureWindowsPath(_powershell("[Environment]::GetFolderPath('LocalApplicationData')")) / "amigent"
    local = _local(str(folder))
    local.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON, local / "amigent.ico")
    files = [_launcher(host), _script(f"Uninstall {NAME}", ["--uninstall"])]
    for name, text in files:
        (local / name).write_text(text, encoding="utf-8")
    for name in OLD_NAMES:
        for suffix in (".vbs", ".pyw"):
            (local / f"{name}{suffix}").unlink(missing_ok=True)
    launcher, remover = (folder / name for name, _ in files)
    icon = folder / "amigent.ico"
    size_kb = sum(f.stat().st_size for f in local.iterdir()) // 1024 + 1
    old = ", ".join(f"'{n}.lnk'" for n in OLD_NAMES)
    script = f"""
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {{
  foreach ($old in @({old})) {{ $p = Join-Path $dir $old; if (Test-Path $p) {{ Remove-Item $p }} }}
  $link = $shell.CreateShortcut((Join-Path $dir '{NAME}.lnk'))
  $link.TargetPath = "{_runner()}"
  $link.Arguments = '"{launcher}"'
  $link.WorkingDirectory = '{folder}'
  $link.IconLocation = '{icon},0'
  $link.Description = 'Watch and drive the Amiga'
  $link.Save()
  $link.FullName
}}
$key = '{REGISTRY}'
New-Item -Path $key -Force | Out-Null
$values = @{{
  DisplayName = '{NAME}'; DisplayVersion = '{__version__}'; Publisher = '{PUBLISHER}'
  DisplayIcon = '{icon}'; InstallLocation = '{folder}'; URLInfoAbout = '{HOMEPAGE}'
  Comments = 'Watch and drive your Amiga over the network'
  UninstallString = '"' + "{_runner()}" + '" "{remover}"'
}}
foreach ($v in $values.GetEnumerator()) {{ Set-ItemProperty -Path $key -Name $v.Key -Value $v.Value }}
foreach ($flag in 'NoModify', 'NoRepair') {{ New-ItemProperty -Path $key -Name $flag -Value 1 -PropertyType DWord -Force | Out-Null }}
New-ItemProperty -Path $key -Name EstimatedSize -Value {size_kb} -PropertyType DWord -Force | Out-Null
"Installed apps: {NAME} {__version__}"
"""
    return _powershell(script).splitlines()


def uninstall() -> list[str]:
    """Remove shortcuts, launcher folder and the Installed apps entry; returns what was removed."""
    names = ", ".join(f"'{n}.lnk'" for n in (NAME, *OLD_NAMES))
    script = f"""
foreach ($dir in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {{
  foreach ($name in @({names})) {{
    $path = Join-Path $dir $name
    if (Test-Path $path) {{ Remove-Item $path; $path }}
  }}
}}
$folder = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'amigent'
if (Test-Path $folder) {{ Remove-Item $folder -Recurse; $folder }}
if (Test-Path '{REGISTRY}') {{ Remove-Item '{REGISTRY}' -Recurse; 'Installed apps: {NAME}' }}
"""
    return _powershell(script).splitlines()
