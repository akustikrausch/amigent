"""The Apollo probe and daemon ship in the package. PIP does not.

The installer and updater are driven here, and the wheel and source
distribution are opened. Nothing in this file contacts an Amiga, and a
matching archive does not show that AUDIO has been heard on hardware.
"""

import hashlib
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

# GCC 6.50 Stable. The daemon links the MMIO guard with AUDIO. build/ is not packaged.
APOLLO = {
    "amigentd.apollo": (128744, "63db83ff021c47f7146e0872cd36bf90eb201f36a0cf455a58f3c518a84e8ef1"),
    "amigent-apollo-probe": (3112, "ab454b39988bd7b88c24058d0398507456ff12ff180b638d626beac821e1aff4"),
}
CLASSIC = {
    "amigentd": (113068, "def99527a1a222afe4b20dc7874264a8413e9aa3fb7eb74e9f11123da0521203"),
    "amigentd.000": (119128, "db566763f1c1033713c0f2afd4e511a96ac154825ab67951c020faa6907464c7"),
    "amigent-monitor": (18376, "78b49713d9529bbe5804f02525f50bfc3e6431011ed9be0ae39c28643759b80e"),
    "amigentd.080": (108896, "8022366042e0c6e970f84acd16d65231d4fa0fb8d25c83f48c0a6221cb414339"),
}
EXCLUDED = ("amigentd.080", "private/", ".git/", ".githooks/", "ammx.s", "build/apollo")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _packaged(name: str) -> bytes:
    return (BIN / name).read_bytes()


def test_the_packaged_apollo_executables_match_the_staged_build():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'amigent = ["amiga/bin/amigentd.080"]' in pyproject
    assert "amigentd.apollo" not in pyproject.split("exclude-package-data", 1)[1]
    for name, (size, digest) in APOLLO.items():
        data = _packaged(name)
        assert len(data) == size and _sha(data) == digest
        assert data.startswith(HUNK)
        staged = ROOT / "build" / "apollo" / name
        if staged.is_file():
            assert staged.read_bytes() == data
    daemon = _packaged("amigentd.apollo")
    assert b"arne.audio" in daemon and b"ApolloOS" in daemon
    # Opcode 49 / PIP picture supply is not implemented; a reserved SYSINFO key
    # token may still appear in the validated binary string table.
    assert b"pip=1" not in daemon
    probe = _packaged("amigent-apollo-probe")
    assert b"ApolloOS" in probe and b"AmigaOS" in probe and b"arne.audio" not in probe
    for name, (size, digest) in CLASSIC.items():
        data = _packaged(name)
        assert len(data) == size and _sha(data) == digest and data.startswith(HUNK)
    assert FILES == ("amigentd", "amigentd.000")


class SetupLink:
    """The SETUP connection: CPU and the probe return codes, everything else succeeds."""

    def __init__(self, cpu=0, probe=0, probe_present=True):
        self.cpu = cpu
        self.probe = probe
        self.probe_present = probe_present
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


def test_setup_copies_the_apollo_daemon_when_the_probe_says_so():
    link = _install(cpu=0, probe=0)
    assert link.files["C:amigentd.new"] == _packaged("amigentd.apollo")
    assert link.files["T:amigent-apollo-probe"] == _packaged("amigent-apollo-probe")
    assert link.files["SYS:amigent/amigent-monitor"] == _packaged("amigent-monitor")
    assert b"pip" not in link.files["T:amigent-setup"]


def test_setup_keeps_the_classic_daemon_when_the_probe_says_amigaos():
    link = _install(cpu=0, probe=5)
    assert link.files["C:amigentd.new"] == _packaged("amigentd")
    assert link.files["T:amigent-apollo-probe"] == _packaged("amigent-apollo-probe")
    assert link.files["SYS:amigent/amigent-monitor"] == _packaged("amigent-monitor")


def test_setup_on_a_68000_never_asks_the_probe():
    link = _install(cpu=5, probe=0)
    assert link.files["C:amigentd.new"] == _packaged("amigentd.000")
    assert "T:amigent-apollo-probe" not in link.files


def test_setup_falls_back_when_the_optional_files_are_absent(tmp_path, monkeypatch):
    package = tmp_path / "amiga"
    shutil.copytree(AMIGA, package)
    (package / "bin" / "amigentd.apollo").unlink()
    (package / "bin" / "amigent-apollo-probe").unlink()
    monkeypatch.setattr(setup, "AMIGA", package)
    link = _install(cpu=0, probe=0)
    assert link.files["C:amigentd.new"] == (package / "bin" / "amigentd").read_bytes()
    assert "T:amigent-apollo-probe" not in link.files

    # 1.5.0: without the Apollo daemon the probe is never sent, even when a probe file exists.
    (package / "bin" / "amigent-apollo-probe").write_bytes(b"probe")
    link = _install(cpu=0, probe=0)
    assert link.files["C:amigentd.new"] == (package / "bin" / "amigentd").read_bytes()
    assert "T:amigent-apollo-probe" not in link.files


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
            return ExecResult(0, "amigentd 1.4.0\n")
        if command.startswith("Run >NIL: rx T:amigent-update.rexx"):
            self.files["C:amigentd"] = self.files.pop("C:amigentd.new")
            self.files["T:amigent-update.done"] = b"new\n"
        return ExecResult(0, "")


def _update(monkeypatch, **kwargs):
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)
    channel = UpdateChannel("192.0.2.1", **kwargs)
    channel.files["C:amigentd"] = b"old"
    monkeypatch.setattr(setup, "AmigaChannel", lambda host, timeout=30: channel)
    assert setup.update("192.0.2.1", None) == 0
    return channel


def test_update_on_vampire_amigaos_keeps_the_classic_daemon(monkeypatch):
    channel = _update(monkeypatch, info={"os": "AmigaOS", "exec_version": "47.4", "attn_flags": "0x400"})
    assert channel.files["C:amigentd"] == _packaged("amigentd")
    assert "T:amigent-apollo-probe" not in channel.files


def test_update_falls_back_when_the_probe_is_unsuccessful(monkeypatch):
    refused = _update(monkeypatch, info={}, probe="AmigaOS\n")
    assert refused.files["C:amigentd"] == _packaged("amigentd")
    assert refused.files["T:amigent-apollo-probe"] == _packaged("amigent-apollo-probe")

    failed = _update(monkeypatch, info={}, fail_probe=True)
    assert failed.files["C:amigentd"] == _packaged("amigentd")


def _names_ending(names, filename):
    return [name for name in names if name.replace("\\", "/").endswith("/" + filename)]


def test_wheel_and_sdist_carry_the_apollo_executables(tmp_path):
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
        for name, (size, digest) in APOLLO.items():
            [member] = _names_ending(names, name)
            data = archive.read(member)
            assert len(data) == size and _sha(data) == digest and data.startswith(HUNK)
        for name in ("amigentd", "amigentd.000", "amigent-monitor"):
            [member] = _names_ending(names, name)
            assert archive.read(member) == _packaged(name)
        meta = archive.read(_names_ending(names, "METADATA")[0]).decode("utf-8")
        requires = [line for line in meta.splitlines() if line.startswith("Requires-Dist:")]
        assert requires and all("apollo" not in line.lower() for line in requires)
        folded = "\n".join(names)
        for banned in EXCLUDED:
            assert banned not in folded
    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
        for name, (size, digest) in APOLLO.items():
            [member] = _names_ending(names, name)
            data = archive.extractfile(member).read()
            assert len(data) == size and _sha(data) == digest
        for name in ("amigentd", "amigentd.000", "amigent-monitor"):
            [member] = _names_ending(names, name)
            assert archive.extractfile(member).read() == _packaged(name)
        folded = "\n".join(names)
        for banned in EXCLUDED:
            assert banned not in folded
        assert any(name.endswith("pyproject.toml") for name in names)
