"""Source-only Apollo packaging policy and safe absent-binary setup behaviour.

Upstream releases ship the classic 68000/68020 binaries only and exclude
prebuilt Apollo binaries (amigentd.apollo, amigent-apollo-probe,
amigent-monitor.apollo, amigentd.080) from wheels, sdists and release archives.
Apollo binaries are compiled from source in CI and via Makefile.apollo.
"""

import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from amigent import setup
from amigent.client import ExecResult
from amigent.errors import AmigaError
from amigent.paths import AMIGA
from amigent.serve import FILES

ROOT = Path(__file__).resolve().parent.parent
BIN = AMIGA / "bin"
HUNK = b"\x00\x00\x03\xf3"
CLASSIC_BINARIES = ("amigentd", "amigentd.000", "amigent-monitor", "amigent-status", "amigent-levels")
APOLLO_BINARIES = ("amigentd.apollo", "amigent-apollo-probe", "amigent-monitor.apollo")
WHEEL_BANNED = (*APOLLO_BINARIES, "amigentd.080", "src/native/", "private/", ".git/", ".githooks/", "ammx.s", "build/apollo")
SDIST_BANNED = (*APOLLO_BINARIES, "private/", ".git/", ".githooks/", "ammx.s", "build/apollo")


def _packaged(name: str) -> bytes:
    return (BIN / name).read_bytes()


def test_source_only_repository_excludes_prebuilt_apollo_binaries():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    excluded_block = pyproject.split("exclude-package-data", 1)[1]
    for name in (*APOLLO_BINARIES, "amigentd.080"):
        assert name in excluded_block
    for name in APOLLO_BINARIES:
        assert not (BIN / name).exists(), f"prebuilt {name} must not ship in upstream"
    for name in CLASSIC_BINARIES:
        data = _packaged(name)
        assert data.startswith(HUNK)
    assert FILES == ("amigentd", "amigentd.000")


def test_guide_and_workflow_document_source_build_and_ci_verification():
    guide = (ROOT / "docs/GUIDE.md").read_text(encoding="utf-8")
    assert "Apollo V4 / 68080 (source-only)" in guide
    assert "Makefile.apollo" in guide
    assert "build-amigentd-apollo" in guide
    workflow = (ROOT / ".github/workflows/amiga.yml").read_text(encoding="utf-8")
    assert "Makefile.apollo" in workflow
    assert "CPU_FLAG=-m68020" in workflow
    assert "amigentd.apollo" in workflow
    assert "amigent-apollo-probe" in workflow
    assert "amigent-monitor.apollo" in workflow


class SetupLink:
    """The SETUP connection: CPU and optional probe return codes, everything else succeeds."""

    def __init__(self, cpu=0, probe=0):
        self.cpu = cpu
        self.probe = probe
        self.files = {}
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        if command.startswith("CPU"):
            return self.cpu
        if "amigent-apollo-probe" in command and not command.startswith("Protect"):
            return self.probe
        return 0

    def put(self, name, data):
        self.files[name] = data


def _install(cpu=0, probe=0):
    link = SetupLink(cpu, probe)
    setup.install(link, "SYS:amigent", "secret", None, False)
    return link


def test_setup_without_apollo_binaries_installs_classic_safely():
    link = _install(cpu=0, probe=0)
    assert link.files["C:amigentd.new"] == _packaged("amigentd")
    assert link.files["SYS:amigent/amigent-monitor"] == _packaged("amigent-monitor")
    assert "T:amigent-apollo-probe" not in link.files


def test_setup_uses_locally_staged_apollo_binaries_when_built(tmp_path, monkeypatch):
    package = tmp_path / "amiga"
    shutil.copytree(AMIGA, package)
    (package / "bin" / "amigentd.apollo").write_bytes(HUNK + b"local-apollo")
    (package / "bin" / "amigent-apollo-probe").write_bytes(HUNK + b"local-probe")
    (package / "bin" / "amigent-monitor.apollo").write_bytes(HUNK + b"local-monitor")
    monkeypatch.setattr(setup, "AMIGA", package)

    apollo_link = _install(cpu=0, probe=0)
    assert apollo_link.files["C:amigentd.new"] == HUNK + b"local-apollo"
    assert apollo_link.files["T:amigent-apollo-probe"] == HUNK + b"local-probe"
    assert apollo_link.files["SYS:amigent/amigent-monitor"] == HUNK + b"local-monitor"

    classic_link = _install(cpu=0, probe=5)
    assert classic_link.files["C:amigentd.new"] == _packaged("amigentd")
    assert classic_link.files["SYS:amigent/amigent-monitor"] == _packaged("amigent-monitor")

    k68000 = _install(cpu=5, probe=0)
    assert k68000.files["C:amigentd.new"] == _packaged("amigentd.000")
    assert "T:amigent-apollo-probe" not in k68000.files


class UpdateChannel:
    protocol = 6

    def __init__(self, host, timeout=30, info=None, version="", probe="AmigaOS\n", fail_probe=False):
        self.host = host
        self.info = info
        self.version = version
        self.probe = probe
        self.fail_probe = fail_probe
        self.files = {}
        self.commands = []

    def connect(self):
        pass

    def close(self):
        pass

    def sysinfo(self):
        if self.info is None:
            raise AttributeError("no sysinfo")
        return self.info

    def read_file(self, path):
        if path not in self.files:
            raise AmigaError("object not found")
        return self.files[path]

    def write_file(self, path, data):
        self.files[path] = data

    def exec(self, command):
        self.commands.append(command)
        if command.startswith("CPU"):
            return ExecResult(0, "")
        if command.startswith("Version FULL"):
            return ExecResult(0, self.version)
        if command.startswith("T:amigent-apollo-probe"):
            if self.fail_probe:
                raise AmigaError("probe failed")
            return ExecResult(0, self.probe)
        if command.startswith("Version"):
            return ExecResult(0, "amigentd 1.5.0\n")
        if command.startswith("Run >NIL: rx T:amigent-update.rexx"):
            self.files["C:amigentd"] = self.files.pop("C:amigentd.new")
            self.files["T:amigent-update.done"] = b"new\n"
        return ExecResult(0, "")


def test_update_on_apolloos_without_packaged_binary_keeps_installed_daemon(monkeypatch, capsys):
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)
    channel = UpdateChannel("192.0.2.1", info={"os": "ApolloOS", "exec_version": "51.1", "attn_flags": "0x400"})
    channel.files["C:amigentd"] = b"installed-apollo"
    monkeypatch.setattr(setup, "AmigaChannel", lambda host, timeout=30: channel)
    assert setup.update("192.0.2.1", None) == 0
    assert channel.files["C:amigentd"] == b"installed-apollo"
    assert "has no amigentd.apollo" in capsys.readouterr().out
    assert "T:amigent-apollo-probe" not in channel.files


def _names_ending(names, filename):
    return [name for name in names if name.replace("\\", "/").endswith("/" + filename)]


def test_wheel_and_sdist_enforce_source_only_apollo_policy(tmp_path):
    out = tmp_path / "dist"
    work = tmp_path / "work"
    out.mkdir()
    script = tmp_path / "build_dist.py"
    script.write_text(
        "import os\n"
        "import setuptools\n"
        "from setuptools.command.build import build as build_command\n"
        "original = build_command.initialize_options\n"
        "def initialize(self):\n"
        "    original(self)\n"
        "    self.build_base = os.environ['AMIGENT_BUILD_BASE']\n"
        "build_command.initialize_options = initialize\n"
        "from setuptools.build_meta import build_sdist, build_wheel\n"
        "import os as _os\n"
        "out = _os.environ['AMIGENT_DIST_DIR']\n"
        "print('WHEEL ' + build_wheel(out))\n"
        "print('SDIST ' + build_sdist(out))\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT, check=False, capture_output=True, text=True,
        env={**os.environ, "AMIGENT_DIST_DIR": str(out), "AMIGENT_BUILD_BASE": str(work)},
    )
    if completed.returncode:
        raise AssertionError(
            f"in-process wheel/sdist build failed ({completed.returncode})\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    produced = dict(line.split(" ", 1) for line in completed.stdout.splitlines() if line.startswith(("WHEEL ", "SDIST ")))
    wheel = out / produced["WHEEL"]
    sdist = out / produced["SDIST"]

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        for name in ("amigentd", "amigentd.000", "amigent-monitor"):
            [member] = _names_ending(names, name)
            assert archive.read(member) == _packaged(name)
        meta = archive.read(_names_ending(names, "METADATA")[0]).decode("utf-8")
        requires = [line for line in meta.splitlines() if line.startswith("Requires-Dist:")]
        assert requires and all("apollo" not in line.lower() for line in requires)
        folded = "\n".join(names)
        for banned in WHEEL_BANNED:
            assert banned not in folded

    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
        for name in ("amigentd", "amigentd.000", "amigent-monitor"):
            [member] = _names_ending(names, name)
            assert archive.extractfile(member).read() == _packaged(name)
        folded = "\n".join(names)
        for banned in SDIST_BANNED:
            assert banned not in folded
        for expected_src in (
            "src/native/amiga/Makefile.apollo",
            "src/native/amiga/build-amigentd-apollo",
            "src/native/amiga/daemon/amigentd.c",
            "src/native/amiga/apollo/apollo_audio.c",
            "src/native/amiga/apollo/apollo_mmio.c",
            "src/native/amiga/apollo/apollo_probe.c",
        ):
            assert any(name.replace("\\", "/").endswith("/" + expected_src) for name in names), expected_src
        assert any(name.endswith("pyproject.toml") for name in names)
