"""ApolloOS identification for the installer, without an Apollo daemon in the package.

The 68080 daemon, AUDIO and PIP are not part of this change. These tests check
which packaged file the installer copies. They do not run on Apollo hardware.
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


def test_update_on_apolloos_keeps_the_packaged_68020_daemon(monkeypatch):
    """The published package has no Apollo daemon, so the copy must stay the classic one."""
    assert not (AMIGA / "bin" / "amigentd.apollo").is_file()
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
    assert amiga["C:amigentd"] == (AMIGA / "bin" / "amigentd").read_bytes()
    assert "T:amigent-apollo-probe" not in amiga


def test_protocol_6_still_ends_at_quiet_and_the_probe_is_68000_only():
    daemon = (ROOT / "amiga" / "amigentd.c").read_text(encoding="utf-8")
    start = daemon.index("CMD_NOTE,")
    names = re.findall(r"CMD_[A-Z0-9]+", daemon[start:daemon.index("};", start)])
    assert names == list(PROTOCOL_6)
    assert "PROTOCOL_VERSION 6" in daemon
    assert "CMD_AUDIO" not in daemon and "CMD_PIP" not in daemon
    assert "AMIGENT_APOLLO" not in daemon

    detect = (ROOT / "amiga" / "apollo" / "apollo_detect.c").read_text(encoding="utf-8")
    probe = (ROOT / "amiga" / "apollo" / "apollo_probe.c").read_text(encoding="utf-8")
    makefile = (ROOT / "amiga" / "Makefile.apollo").read_text(encoding="utf-8")
    assert "apollo_os_here" in detect and "VREG_BOARD" in detect
    assert "CMD_AUDIO" not in detect and "CMD_PIP" not in detect
    assert "RETURN_WARN" in probe and "m68080" not in probe
    assert "amigent-apollo-probe" in makefile
    assert "amigentd.apollo" not in makefile and "apollo.c" not in makefile
    assert "-m68080" not in makefile and "-m68000" in makefile
