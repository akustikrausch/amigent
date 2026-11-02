"""Install the viewer as a program: on Windows in the Start menu, on the
desktop and in Installed apps; on macOS as an app in ~/Applications.

Works from WSL, from a Windows Python and on macOS. The program starts the
viewer without a console window; it ends again when its window is closed.
"""

from __future__ import annotations

import getpass
import os
import platform
import shlex
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


def _ps(text) -> str:
    """Text for inside single quotes in PowerShell."""
    return str(text).replace("'", "''")


def _runner() -> str:
    """The program that runs our scripts without a console window."""
    if _wsl():
        return r"$env:WINDIR\System32\wscript.exe"
    return str(Path(sys.executable).with_name("pythonw.exe"))


MAC_APP = Path.home() / "Applications" / f"{NAME}.app"


def _install_mac(host: str) -> list[str]:
    """~/Applications/Amigent.app: a shell script that starts the viewer."""
    settings = {k: os.environ[k] for k in SETTINGS if os.environ.get(k)}
    package_root = str(Path(__file__).resolve().parents[1])
    contents = MAC_APP / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(exist_ok=True)
    shutil.copyfile(ICON.with_suffix(".icns"), contents / "Resources" / "amigent.icns")
    exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in settings.items())
    launcher = contents / "MacOS" / NAME
    launcher.write_text(
        "#!/bin/sh\n"
        f"export PYTHONPATH={shlex.quote(package_root)}\n{exports}"
        f"exec {shlex.quote(sys.executable)} -m amigent.viewer {shlex.quote(host)} --exit-when-closed\n")
    launcher.chmod(0o755)
    (contents / "Info.plist").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>{NAME}</string>
  <key>CFBundleDisplayName</key><string>{NAME}</string>
  <key>CFBundleIdentifier</key><string>de.akustikrausch.amigent</string>
  <key>CFBundleVersion</key><string>{__version__}</string>
  <key>CFBundleShortVersionString</key><string>{__version__}</string>
  <key>CFBundleExecutable</key><string>{NAME}</string>
  <key>CFBundleIconFile</key><string>amigent</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict></plist>
""")
    subprocess.run(["touch", str(MAC_APP)], check=False)  # Finder picks up the icon
    return [str(MAC_APP)]


def install(host: str) -> list[str]:
    """Create launcher, shortcuts and the Installed apps entry; returns what was created."""
    if sys.platform == "darwin":
        return _install_mac(host)
    if not (_wsl() or sys.platform == "win32"):
        raise SystemExit("this is for Windows and macOS; elsewhere run amigent-viewer directly")
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
  $link.Arguments = '"{_ps(launcher)}"'
  $link.WorkingDirectory = '{_ps(folder)}'
  $link.IconLocation = '{_ps(icon)},0'
  $link.Description = 'Watch and drive the Amiga'
  $link.Save()
  $link.FullName
}}
$key = '{REGISTRY}'
New-Item -Path $key -Force | Out-Null
$values = @{{
  DisplayName = '{NAME}'; DisplayVersion = '{__version__}'; Publisher = '{PUBLISHER}'
  DisplayIcon = '{_ps(icon)}'; InstallLocation = '{_ps(folder)}'; URLInfoAbout = '{HOMEPAGE}'
  Comments = 'Watch and drive your Amiga over the network'
  UninstallString = '"' + "{_runner()}" + '" "{_ps(remover)}"'
}}
foreach ($v in $values.GetEnumerator()) {{ Set-ItemProperty -Path $key -Name $v.Key -Value $v.Value }}
foreach ($flag in 'NoModify', 'NoRepair') {{ New-ItemProperty -Path $key -Name $flag -Value 1 -PropertyType DWord -Force | Out-Null }}
New-ItemProperty -Path $key -Name EstimatedSize -Value {size_kb} -PropertyType DWord -Force | Out-Null
"Installed apps: {NAME} {__version__}"
"""
    return _powershell(script).splitlines()


def uninstall() -> list[str]:
    """Remove shortcuts, launcher folder and the Installed apps entry; returns what was removed."""
    if sys.platform == "darwin":
        if MAC_APP.exists():
            shutil.rmtree(MAC_APP)
            return [str(MAC_APP)]
        return []
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
