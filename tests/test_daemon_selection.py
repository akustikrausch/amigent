"""Compatible daemon selection and update protection.

These tests use the packaged files and stand-ins. They do not contact an Amiga.
"""

from amigent import setup
from amigent.client import ExecResult
from amigent.daemons import (
    CPU_68000, CPU_68010, CPU_68020, CPU_68030, CPU_68040, CPU_68060, CPU_68080,
    EXPERIMENTAL, Machine, available_names, choose, consider_replacement, detect_cpu,
    embedded_version, identify,
)
from amigent.errors import AmigaError
from amigent.paths import AMIGA

BIN = AMIGA / "bin"


def _answers(table):
    def run(command):
        for prefix, code in table:
            if command.startswith(prefix):
                return code
        return 10
    return run


def test_cpu_checks_stop_at_the_first_cpu_the_machine_lacks():
    assert detect_cpu(_answers([("CPU CHECK 68020", 5), ("CPU CHECK 68010", 5)])) == CPU_68000
    assert detect_cpu(_answers([("CPU CHECK 68020", 5), ("CPU CHECK 68010", 0)])) == CPU_68010
    assert detect_cpu(_answers([("CPU CHECK 68020", 0), ("CPU CHECK 68030", 5)])) == CPU_68020
    assert detect_cpu(_answers([
        ("CPU CHECK 68020", 0), ("CPU CHECK 68030", 0), ("CPU CHECK 68040", 0), ("CPU CHECK 68060", 5),
    ])) == CPU_68040
    assert detect_cpu(_answers([("CPU CHECK 68020", 0), ("CPU CHECK 68030", 10)])) == CPU_68020
    assert detect_cpu(_answers([("CPU CHECK 68020", 20)])) is None


def test_each_cpu_gets_a_verified_compatible_daemon():
    present = available_names(BIN)
    assert choose(Machine(CPU_68000), present).name == "amigentd.000"
    assert choose(Machine(CPU_68010), present).name == "amigentd.000"
    for cpu in (CPU_68020, CPU_68030, CPU_68040, CPU_68060, CPU_68080):
        assert choose(Machine(cpu), present).name == "amigentd"
    assert choose(Machine(None), present).name == "amigentd.000"
    assert "amigentd.080" in present
    experimental = identify((BIN / "amigentd.080").read_bytes(), BIN)
    assert experimental is not None and experimental.status == EXPERIMENTAL
    assert choose(Machine(CPU_68080), present).name != "amigentd.080"


def test_a_missing_preferred_build_falls_back_and_an_empty_set_does_not():
    assert choose(Machine(CPU_68020), {"amigentd.000"}).name == "amigentd.000"
    assert choose(Machine(CPU_68000), {"amigentd"}) is None
    assert choose(Machine(None), set()) is None


def test_installer_falls_back_or_stops_when_files_are_missing(tmp_path, monkeypatch):
    package = tmp_path / "amiga"
    binary = package / "bin"
    binary.mkdir(parents=True)
    (binary / "amigentd.000").write_bytes(b"sixty-eight-thousand")
    monkeypatch.setattr(setup, "AMIGA", package)
    assert setup._chosen(Machine(CPU_68020)) == "amigentd.000"
    assert setup._chosen(Machine(None)) == "amigentd.000"
    (binary / "amigentd.000").unlink()
    try:
        setup._chosen(Machine(CPU_68020))
    except SystemExit as stopped:
        assert "no compatible" in str(stopped)
    else:
        raise AssertionError("a machine with no compatible daemon was given a build")


def test_updates_keep_unknown_or_newer_and_skip_identical_bytes(tmp_path, monkeypatch):
    package = tmp_path / "amiga"
    binary = package / "bin"
    binary.mkdir(parents=True)
    classic = b"$VER: amigentd 1.4.0 (1.1.2026)\x00 classic"
    older = b"$VER: amigentd 1.3.0 (1.1.2020)\x00 classic"
    same = b"$VER: amigentd 1.4.0 (1.1.2026)\x00 custom"
    newer = b"$VER: amigentd 1.5.0 (1.1.2027)\x00 classic"
    experimental = b"$VER: amigentd 1.4.0 (1.1.2026)\x00 experimental-080"
    (binary / "amigentd").write_bytes(classic)
    (binary / "amigentd.080").write_bytes(experimental)
    monkeypatch.setattr(setup, "AMIGA", package)

    replace, reason = consider_replacement(older, classic, "amigentd", binary)
    assert replace and reason == ""
    keep, reason = consider_replacement(same, classic, "amigentd", binary)
    assert not keep and "same version" in reason
    keep, reason = consider_replacement(newer, classic, "amigentd", binary)
    assert not keep and "newer" in reason
    keep, reason = consider_replacement(classic, classic, "amigentd", binary)
    assert not keep and "same executable" in reason
    keep, reason = consider_replacement(None, classic, "amigentd", binary)
    assert not keep and "could not be read" in reason
    replace, reason = consider_replacement(experimental, classic, "amigentd", binary)
    assert replace and reason == ""
    assert embedded_version(older) == (1, 3, 0)


class Channel:
    protocol = 4

    def __init__(self, installed, cpu_rc=0):
        self.files = {"C:amigentd": installed}
        self.cpu_rc = cpu_rc
        self.written = []

    def connect(self):
        pass

    def close(self):
        pass

    def read_file(self, path):
        if path not in self.files:
            raise AmigaError("object not found")
        return self.files[path]

    def write_file(self, path, data):
        self.written.append(path)
        self.files[path] = data

    def exec(self, command):
        if command.startswith("CPU"):
            return ExecResult(self.cpu_rc, "")
        if command.startswith("Version"):
            return ExecResult(0, "amigentd 1.4.0\n")
        return ExecResult(0, "")


def test_update_keeps_a_newer_unknown_daemon(tmp_path, monkeypatch):
    package = tmp_path / "amiga"
    binary = package / "bin"
    binary.mkdir(parents=True)
    (binary / "amigentd").write_bytes(b"$VER: amigentd 1.4.0 (1.1.2026)\x00 classic")
    (binary / "amigentd.000").write_bytes(b"sixty-eight-thousand")
    installed = b"$VER: amigentd 1.5.0 (1.1.2027)\x00 custom"
    monkeypatch.setattr(setup, "AMIGA", package)
    monkeypatch.setattr(setup.time, "sleep", lambda _seconds: None)
    channel = Channel(installed)
    monkeypatch.setattr(setup, "AmigaChannel", lambda host, timeout=30: channel)
    assert setup.update("192.0.2.1", None) == 0
    assert channel.files["C:amigentd"] == installed
    assert "C:amigentd.new" not in channel.files
