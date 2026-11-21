"""Which packaged daemon the installer copies.

These tests do not run on Apollo hardware. PIP is not a packaged capability.
"""

import re
from pathlib import Path

import pytest

from amigent import setup
from amigent.client import ExecResult
from amigent.errors import AmigaError
from amigent.paths import AMIGA

ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_6 = (
    "CMD_NOTE", "CMD_LOADED", "CMD_PEEK", "CMD_TASKS", "CMD_TASKCTL", "CMD_POKE",
    "CMD_TRACE", "CMD_LOCKS", "CMD_CPU", "CMD_DRAG", "CMD_QUIET",
)


def test_apolloos_is_the_name_or_exec_51_with_the_fpga_bit():
    assert setup.apollo_os({"os": "ApolloOS"})
    assert setup.apollo_os({}, "exec.library 51.1 (ApolloOS)")
    assert setup.apollo_os({"exec_version": "51.16", "attn_flags": "0x400"})
    assert setup.apollo_os({"exec_version": "51", "attn_flags": str(setup.AFF_FPGA)})
    assert not setup.apollo_os({"os": "AmigaOS", "exec_version": "47.4", "attn_flags": "0x400"})
    assert not setup.apollo_os({"exec_version": "47", "attn_flags": "0x400"})
    assert not setup.apollo_os({"exec_version": "51.1", "attn_flags": "0"})
    assert not setup.apollo_os({})
    assert not setup.apollo_os({"exec_version": "nope", "attn_flags": "nope"})


def test_a_68000_stays_on_its_build_even_when_the_text_says_apolloos():
    assert setup.select_build(5, {"os": "ApolloOS"}) == "amigentd.000"


def test_apolloos_uses_the_optional_daemon_only_when_it_is_packaged(tmp_path, monkeypatch):
    package = tmp_path / "amiga"
    binary = package / "bin"
    binary.mkdir(parents=True)
    (binary / "amigentd").write_bytes(b"classic")
    monkeypatch.setattr(setup, "AMIGA", package)

    assert setup.select_build(0, {"os": "ApolloOS"}) == "amigentd"
    (binary / "amigentd.apollo").write_bytes(b"apollo")
    assert setup.select_build(0, {"os": "ApolloOS"}) == "amigentd.apollo"
    assert setup.select_build(0, {"exec_version": "51.1", "attn_flags": "0x400"}) == "amigentd.apollo"
    assert setup.select_build(0, {}, "ApolloOS") == "amigentd.apollo"
    assert setup.select_build(0, {}) == "amigentd"


def test_an_unknown_cpu_stops_before_a_build_is_chosen():
    with pytest.raises(SystemExit):
        setup.select_build(20, {"os": "ApolloOS"})


class Link:
    def __init__(self, code=0, probe=10):
        self.code = code
        self.probe = probe
        self.puts = []

    def run(self, cmd):
        if cmd.startswith("CPU"):
            return self.code
        if cmd.startswith("T:amigent-apollo-probe") and "Protect" not in cmd:
            return self.probe
        return 0

    def put(self, name, data):
        self.puts.append(name)


def test_setup_keeps_the_classic_daemon_when_the_probe_or_binary_is_absent(tmp_path, monkeypatch):
    package = tmp_path / "amiga"
    binary = package / "bin"
    binary.mkdir(parents=True)
    monkeypatch.setattr(setup, "AMIGA", package)
    link = Link(0)

    assert setup.amigentd_build(link) == "amigentd"
    assert link.puts == []

    (binary / "amigent-apollo-probe").write_bytes(b"probe")
    link = Link(0, probe=0)
    assert setup.amigentd_build(link) == "amigentd"
    assert "T:amigent-apollo-probe" in link.puts

    (binary / "amigentd.apollo").write_bytes(b"apollo")
    assert setup.amigentd_build(Link(0, probe=0)) == "amigentd.apollo"
    assert setup.amigentd_build(Link(0, probe=5)) == "amigentd"
    assert setup.amigentd_build(Link(5, probe=0)) == "amigentd.000"


class Channel:
    def __init__(self, cpu=0, info=None, version="", probe="", fail_probe=False):
        self.cpu = cpu
        self.info = info
        self.version = version
        self.probe = probe
        self.fail_probe = fail_probe
        self.commands = []
        self.files = {}

    def exec(self, command):
        self.commands.append(command)
        if command.startswith("CPU"):
            return ExecResult(self.cpu, "")
        if command.startswith("Version"):
            return ExecResult(0, self.version)
        if command.startswith("T:amigent-apollo-probe"):
            if self.fail_probe:
                raise AmigaError("probe failed")
            return ExecResult(0, self.probe)
        return ExecResult(0, "")

    def sysinfo(self):
        if self.info is None:
            raise AttributeError("no sysinfo")
        return self.info

    def write_file(self, path, data):
        self.files[path] = data


def test_a_running_daemon_is_trusted_over_the_probe(tmp_path, monkeypatch):
    package = tmp_path / "amiga"
    binary = package / "bin"
    binary.mkdir(parents=True)
    (binary / "amigentd").write_bytes(b"classic")
    (binary / "amigentd.apollo").write_bytes(b"apollo")
    (binary / "amigent-apollo-probe").write_bytes(b"probe")
    monkeypatch.setattr(setup, "AMIGA", package)

    vampire = Channel(info={"os": "AmigaOS", "exec_version": "47.4", "attn_flags": "0x400"},
                      probe="ApolloOS\n")
    assert setup.build_for_channel(vampire) == "amigentd"
    assert "T:amigent-apollo-probe" not in vampire.files

    identified = Channel(info={"os": "ApolloOS", "exec_version": "51.1", "attn_flags": "0x400"})
    assert setup.build_for_channel(identified) == "amigentd.apollo"
    assert not any(cmd.startswith("Version") for cmd in identified.commands)

    bare = Channel(info={}, probe="ApolloOS\n")
    assert setup.build_for_channel(bare) == "amigentd.apollo"
    assert bare.files["T:amigent-apollo-probe"] == b"probe"

    broken = Channel(info={}, fail_probe=True)
    assert setup.build_for_channel(broken) == "amigentd"


def test_update_on_identified_apolloos_copies_the_packaged_daemon(monkeypatch):
    """SYSINFO already says ApolloOS, so the updater copies that daemon and does not run the probe."""
    apollo = (AMIGA / "bin" / "amigentd.apollo").read_bytes()
    assert apollo.startswith(b"\x00\x00\x03\xf3")
    assert b"pip=1" not in apollo
    amiga = {"C:amigentd": b"old", "commands": []}

    class Live(Channel):
        protocol = 6

        def __init__(self, host, timeout=30):
            super().__init__(info={"os": "ApolloOS", "exec_version": "51.1", "attn_flags": "0x400"})
            self.host = host

        def connect(self):
            pass

        def close(self):
            pass

        def read_file(self, path):
            if path not in amiga and path not in self.files:
                raise AmigaError("object not found")
            return amiga.get(path, self.files.get(path))

        def write_file(self, path, data):
            amiga[path] = data
            self.files[path] = data

        def exec(self, command):
            amiga["commands"].append(command)
            if command.startswith("Run >NIL: rx T:amigent-update.rexx"):
                amiga["C:amigentd"] = amiga.pop("C:amigentd.new")
                amiga["T:amigent-update.done"] = b"new\n"
            return super().exec(command)

    monkeypatch.setattr(setup.time, "sleep", lambda s: None)
    monkeypatch.setattr(setup, "AmigaChannel", Live)
    assert setup.update("192.0.2.1", None) == 0
    assert amiga["C:amigentd"] == apollo
    assert "T:amigent-apollo-probe" not in amiga


def test_protocol_6_still_ends_at_quiet_and_the_probe_stays_68000():
    daemon = (ROOT / "amiga" / "amigentd.c").read_text(encoding="utf-8")
    start = daemon.index("CMD_NOTE,")
    names = re.findall(r"CMD_[A-Z0-9]+", daemon[start:daemon.index("};", start)])
    assert names == list(PROTOCOL_6) + ["CMD_AUDIO"]
    assert "PROTOCOL_VERSION 6" in daemon
    quiet = daemon.index("CMD_QUIET")
    audio = daemon.index("CMD_AUDIO = 48", quiet)
    assert "#ifdef AMIGENT_APOLLO" in daemon[quiet:audio]
    assert "CMD_PIP" not in daemon

    detect = (ROOT / "amiga" / "apollo" / "apollo_detect.c").read_text(encoding="utf-8")
    probe = (ROOT / "amiga" / "apollo" / "apollo_probe.c").read_text(encoding="utf-8")
    makefile = (ROOT / "amiga" / "Makefile.apollo").read_text(encoding="utf-8")
    assert "apollo_os_here" in detect and "VREG_BOARD" in detect
    assert "CMD_AUDIO" not in detect and "CMD_PIP" not in detect
    assert "RETURN_WARN" in probe and "m68080" not in probe
    assert "amigent-apollo-probe" in makefile and "PROBE_CFLAGS" in makefile
    assert "-m68000" in makefile
    assert "apollo.c" not in makefile and "ammx.s" not in makefile


def test_the_apollo_daemon_reports_its_identity_and_audio_but_not_pip():
    """SYSINFO on the 68080 build names the OS, the board, and audio=1. PIP stays absent."""
    daemon = (ROOT / "amiga" / "amigentd.c").read_text(encoding="utf-8")
    cpu = daemon.index('t_pair(&t, "cpu", 68080)')
    assert "#ifdef AMIGENT_APOLLO" in daemon[cpu - 80:cpu]
    ladder = daemon.index("AFF_68060", cpu)
    assert "#else" in daemon[cpu:ladder]
    assert "#endif" in daemon[ladder:ladder + 220]
    identity = daemon.index("apollo_board()", cpu)
    block = daemon[identity:identity + 800]
    assert "apollo_os_here()" in block and '"os="' in block
    assert '"board="' in block and '"board_name="' in block
    assert 't_pair(&t, "audio", 1)' in block
    assert "pip" not in block
    assert 't_pair(&t, "pip"' not in daemon
    assert "#include <inline/cybergraphics_protos.h>" in daemon
    assert "#include <proto/cybergraphics.h>" not in daemon
    support = (ROOT / "amiga" / "apollo" / "gcc_support.c").read_text(encoding="utf-8")
    for name in ("GetCyberMapAttr", "ReadPixelArray", "LockBitMapTagList", "UnLockBitMap"):
        assert name in support
    makefile = (ROOT / "amiga" / "Makefile.apollo").read_text(encoding="utf-8")
    assert "amigentd.apollo" in makefile and "-m68080" in makefile and "-DAMIGENT_APOLLO" in makefile
    assert "gcc_support.o" in makefile and "div64.o" in makefile
    assert "apollo.o" not in makefile and "apollo_mmio.o" in makefile and "apollo_audio.o" in makefile
