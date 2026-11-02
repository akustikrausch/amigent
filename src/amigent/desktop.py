"""Install the viewer as a real program: Start menu, desktop and Installed apps
on Windows; an app in ~/Applications on macOS.

Runs from WSL, from a Windows Python and on macOS. The program boots the viewer
without a console window and quits when its window closes. No DOS box in sight.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

from . import __version__
from .platform_util import in_wsl as _wsl  # tests swap _wsl, so the alias stays
from .platform_util import windows_exe

NAME = "Amigent"
OLD_NAMES = ("Amiga Remote",)  # shortcuts from older releases, swept away on install
PUBLISHER = "Akustikrausch"
HOMEPAGE = "https://github.com/akustikrausch/amigent"
REGISTRY = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\amigent"
ICON = Path(__file__).with_name("web") / "amigent.ico"
SETTINGS = ("AMIGENT_CTL_PORT", "AMIGENT_SCREEN_PORT", "AMIGENT_VIEWER_PORT", "AMIGENT_FLUSH_VOLUMES")


def _powershell(script: str) -> str:
    exe = windows_exe("powershell.exe") if _wsl() else "powershell"
    cwd = "/mnt/c" if _wsl() and os.path.isdir("/mnt/c") else None  # spare Windows a UNC working directory
    result = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, cwd=cwd, check=True)
    return result.stdout.strip()


def _local(windows_path: str) -> Path:
    """A Windows path as this side can write it (wslpath under WSL)."""
    if not _wsl():
        return Path(windows_path)
    return Path(subprocess.run(["wslpath", "-u", windows_path], capture_output=True, text=True,
                               check=True).stdout.strip())


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg or not arg else arg


def _script(name: str, args: list[str]) -> tuple[str, str]:
    """File name and body of a script that runs amigent-viewer with args, console-free."""
    settings = _settings()
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
    """Escape text for PowerShell single quotes: ' becomes ''."""
    return str(text).replace("'", "''")


def _runner() -> str:
    """The host that runs our scripts with no console window: wscript or pythonw."""
    if _wsl():
        return r"$env:WINDIR\System32\wscript.exe"
    return str(Path(sys.executable).with_name("pythonw.exe"))


MAC_APP = Path.home() / "Applications" / f"{NAME}.app"
MAC_SOURCE = Path(__file__).with_name("macos")
MAC_RELEASE = MAC_SOURCE / "Amigent.zip"  # signed and notarized, out of scripts/build-mac-app.sh
MAC_CONFIG = Path.home() / ".config" / "amigent" / "app.json"
BUNDLE_ID = "de.akustikrausch.amigent"


def _plist() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>{NAME}</string>
  <key>CFBundleDisplayName</key><string>{NAME}</string>
  <key>CFBundleIdentifier</key><string>{BUNDLE_ID}</string>
  <key>CFBundleVersion</key><string>{__version__}</string>
  <key>CFBundleShortVersionString</key><string>{__version__}</string>
  <key>CFBundleExecutable</key><string>{NAME}</string>
  <key>CFBundleIconFile</key><string>amigent</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHumanReadableCopyright</key><string>{PUBLISHER}, MIT License</string>
  <key>NSLocalNetworkUsageDescription</key><string>Amigent connects to your Amiga on the home network.</string>
  <key>NSAppleEventsUsageDescription</key><string>Amigent brings its open window to the front when you start it again.</string>
</dict></plist>
"""


ENTITLEMENTS = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>com.apple.security.automation.apple-events</key><true/>
</dict></plist>
"""


def _settings() -> dict:
    return {k: os.environ[k] for k in SETTINGS if os.environ.get(k)}


def _compile_launcher(out: Path, universal: bool) -> bool:
    """Build the native launcher with swiftc (lipo'd fat when universal); False without Xcode tools."""
    swiftc = shutil.which("swiftc")
    if not swiftc and shutil.which("xcrun"):
        found = subprocess.run(["xcrun", "--find", "swiftc"], capture_output=True, text=True)
        swiftc = found.stdout.strip() if found.returncode == 0 else None
    if not swiftc:
        return False
    source = str(MAC_SOURCE / "Amigent.swift")
    arches = ("arm64", "x86_64") if universal else (platform.machine() or "arm64",)
    parts = []
    for arch in arches:
        part = out.with_name(f"{out.name}-{arch}")
        built = subprocess.run([swiftc, "-O", "-target", f"{arch}-apple-macos12", "-o", str(part), source],
                               capture_output=True, text=True)
        if built.returncode != 0:
            print(built.stderr.strip(), file=sys.stderr)
            return False
        parts.append(part)
    if len(parts) == 1:
        parts[0].replace(out)
    else:
        subprocess.run(["lipo", "-create", "-output", str(out), *map(str, parts)], check=True)
        for part in parts:
            part.unlink()
    return True


def _script_launcher(out: Path, host: str) -> None:
    """No Xcode tools, no problem: a shell script boots the viewer the same way."""
    exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in _settings().items())
    package_root = str(Path(__file__).resolve().parents[1])
    out.write_text(
        "#!/bin/sh\n"
        f"export PYTHONPATH={shlex.quote(package_root)}\n{exports}"
        f"exec {shlex.quote(sys.executable)} -m amigent.viewer {shlex.quote(host)} --exit-when-closed\n")
    out.chmod(0o755)


def build_mac_app(folder: Path, sign: str = "-", universal: bool = False, host: str = "") -> Path:
    """Link Amigent.app together in folder and sign it (sign "-" means ad-hoc)."""
    app = folder / f"{NAME}.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    shutil.copyfile(ICON.with_suffix(".icns"), contents / "Resources" / "amigent.icns")
    (contents / "Info.plist").write_text(_plist())
    executable = contents / "MacOS" / NAME
    native = _compile_launcher(executable, universal)
    if not native:
        _script_launcher(executable, host)
    if shutil.which("codesign"):
        command = ["codesign", "--force", "--sign", sign]
        if sign != "-":
            entitlements = folder / "amigent.entitlements"
            entitlements.write_text(ENTITLEMENTS)
            command += ["--options", "runtime", "--timestamp", "--entitlements", str(entitlements)]
        subprocess.run([*command, str(app)], check=sign != "-",
                       stdout=subprocess.DEVNULL, stderr=None if sign != "-" else subprocess.DEVNULL)
    return app


def _write_mac_config(host: str) -> None:
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[1]), **_settings()}
    MAC_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    MAC_CONFIG.write_text(json.dumps({"python": sys.executable, "args": [host], "env": env}, indent=1))


def _install_mac(host: str) -> list[str]:
    """~/Applications/Amigent.app: the signed release if it shipped, else a fresh local build."""
    _write_mac_config(host)
    MAC_APP.parent.mkdir(parents=True, exist_ok=True)
    if MAC_RELEASE.exists() and shutil.which("ditto"):
        if MAC_APP.exists():
            shutil.rmtree(MAC_APP)
        subprocess.run(["ditto", "-x", "-k", str(MAC_RELEASE), str(MAC_APP.parent)], check=True)
    else:
        build_mac_app(MAC_APP.parent, host=host)
    subprocess.run(["touch", str(MAC_APP)], check=False)  # poke Finder into showing the icon
    if shutil.which("mdimport"):
        subprocess.run(["mdimport", str(MAC_APP)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # for Spotlight
    return [str(MAC_APP)]


def install(host: str) -> list[str]:
    """Lay down launcher, shortcuts and the Installed apps entry; returns what landed."""
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
    """Wipe shortcuts, launcher folder and the Installed apps entry; returns what went."""
    if sys.platform == "darwin":
        removed = []
        for path in (MAC_APP, MAC_CONFIG):
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            else:
                continue
            removed.append(str(path))
        return removed
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


def main() -> None:
    """python -m amigent.desktop --build-mac-app FOLDER [--sign IDENTITY] [--universal]"""
    import argparse
    parser = argparse.ArgumentParser(prog="python -m amigent.desktop")
    parser.add_argument("--build-mac-app", metavar="FOLDER", required=True)
    parser.add_argument("--sign", default="-", help="codesign identity (default: ad-hoc)")
    parser.add_argument("--universal", action="store_true", help="arm64 and x86_64")
    args = parser.parse_args()
    folder = Path(args.build_mac_app)
    folder.mkdir(parents=True, exist_ok=True)
    app = build_mac_app(folder, args.sign, args.universal)
    if (app / "Contents" / "MacOS" / NAME).read_bytes()[:2] == b"#!":
        raise SystemExit("swiftc missing: install the Xcode command line tools for a release build")
    print(app)


if __name__ == "__main__":
    main()
