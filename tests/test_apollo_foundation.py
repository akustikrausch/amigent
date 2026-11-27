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
    "CMD_TRACE", "CMD_LOCKS", "CMD_CPU", "CMD_DRAG", "CMD_QUIET", "CMD_EMU68",
    "CMD_AUDIO",
)


def test_apolloos_is_the_name_or_exec_51_with_the_68080_bit():
    assert setup.apollo_os({"os": "ApolloOS"})
    assert setup.apollo_os({}, "exec.library 51.1 (ApolloOS)")
    assert setup.apollo_os({"exec_version": "51.16", "attn_flags": "0x400"})
    assert setup.apollo_os({"exec_version": "51", "attn_flags": str(setup.AFF_68080)})
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
    (binary / "amigentd").write_bytes(b"classic")
    (binary / "amigentd.000").write_bytes(b"sixty-eight-thousand")
    monkeypatch.setattr(setup, "AMIGA", package)
    link = Link(0)

    assert setup.amigentd_build(link) == "amigentd"
    assert link.puts == []

    (binary / "amigent-apollo-probe").write_bytes(b"probe")
    link = Link(0, probe=0)
    assert setup.amigentd_build(link) == "amigentd"
    assert link.puts == []  # without an Apollo build the probe is not even sent

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


def test_update_on_apolloos_keeps_the_daemon_there_unless_forced(monkeypatch, capsys):
    """The published package has no Apollo daemon. The one on an ApolloOS machine may be a
    68080 build of its own, so an update leaves it; --force puts the classic one on."""
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
    assert amiga["C:amigentd"] == b"old" and "C:amigentd.new" not in amiga
    assert "has no amigentd.apollo" in capsys.readouterr().out
    assert setup.update("192.0.2.1", None, force=True) == 0
    assert amiga["C:amigentd"] == (AMIGA / "bin" / "amigentd").read_bytes()
    assert "T:amigent-apollo-probe" not in amiga


def test_protocol_6_audio_and_probe_is_68000_only():
    daemon = (ROOT / "src/native/amiga/daemon/amigentd.c").read_text(encoding="utf-8")
    start = daemon.index("CMD_NOTE,")
    names = re.findall(r"CMD_[A-Z0-9]+", daemon[start:daemon.index("};", start)])
    assert names == list(PROTOCOL_6)
    assert names.index("CMD_EMU68") + 37 == 48  # CMD_NOTE is 37 -> CMD_EMU68 is 48
    assert names.index("CMD_AUDIO") + 37 == 49  # CMD_AUDIO is unique at 49
    assert "PROTOCOL_VERSION 6" in daemon
    assert "CMD_AUDIO" in daemon and "CMD_PIP" not in daemon
    assert "AMIGENT_APOLLO" in daemon

    detect = (ROOT / "src/native/amiga/apollo/apollo_detect.c").read_text(encoding="utf-8")
    probe = (ROOT / "src/native/amiga/apollo/apollo_probe.c").read_text(encoding="utf-8")
    makefile = (ROOT / "src/native/amiga/Makefile.apollo").read_text(encoding="utf-8")
    assert "apollo_os_here" in detect and "VREG_BOARD" in detect
    assert "CMD_AUDIO" not in detect and "CMD_PIP" not in detect
    assert "RETURN_WARN" in probe and "AFF_FPGA" in probe
    assert "amigent-apollo-probe" in makefile
    assert "amigentd.apollo" in makefile
    assert "amigent-monitor.apollo" in makefile
    assert "-m68000" in makefile and "-m68080" in makefile
    assert not (AMIGA / "bin" / "amigentd.apollo").exists()


def test_an_update_asks_nothing_about_apolloos_without_an_apollo_build(tmp_path, monkeypatch):
    package = tmp_path / "amiga"
    binary = package / "bin"
    binary.mkdir(parents=True)
    (binary / "amigentd").write_bytes(b"classic")
    (binary / "amigent-apollo-probe").write_bytes(b"probe")
    monkeypatch.setattr(setup, "AMIGA", package)
    bare = Channel(info={}, probe="ApolloOS\n")
    assert setup.build_for_channel(bare) == "amigentd"
    assert bare.commands == ["CPU CHECK 68020"] and bare.files == {}


def test_a_described_amiga_is_not_asked_for_its_exec_version(tmp_path, monkeypatch):
    package = tmp_path / "amiga"
    binary = package / "bin"
    binary.mkdir(parents=True)
    for name in ("amigentd", "amigentd.apollo", "amigent-apollo-probe"):
        (binary / name).write_bytes(name.encode())
    monkeypatch.setattr(setup, "AMIGA", package)
    for info, build in (({"exec_version": "47.4", "attn_flags": "0x400"}, "amigentd"),
                        ({"exec_version": "51.1", "attn_flags": "0x400"}, "amigentd.apollo"),
                        ({"exec_version": "51.1", "attn_flags": "0x8f"}, "amigentd")):
        channel = Channel(info=info)
        assert setup.build_for_channel(channel) == build
        assert channel.commands == ["CPU CHECK 68020"] and channel.files == {}


def test_the_c_detection_reads_only_exec_and_never_the_board_register():
    detect = (ROOT / "src/native/amiga/apollo/apollo_detect.c").read_text(encoding="utf-8")
    body = detect.split("BOOL apollo_os_here(void)", 1)[1]
    assert "AFF_68080 (1L << 10)" in detect and "AFF_FPGA" not in detect
    assert "lib_Version >= 51" in body and "AttnFlags & AFF_68080" in body
    assert "apollo_board" not in body and "VREG_BOARD" not in body and "lib_IdString" not in body


def test_apolloos_and_aros_are_not_taken_for_amigaos_4():
    from amigent.system import describe_system, native_ppc, reconcile_system

    apollo = describe_system("Kickstart 51.16, Workbench 51.1\n", "System: 68080 68882\n")
    assert apollo["os"] == "ApolloOS" and not native_ppc(apollo)
    named = describe_system("ApolloOS R9.3\nKickstart 51.16\n", "")
    assert named["os"] == "ApolloOS" and not native_ppc(named)
    aros = describe_system("AROS\nKickstart 51.1\n", "")
    assert aros["os"] == "AROS" and not native_ppc(aros)
    aros68k = describe_system("Kickstart 51.1, Workbench 51.1\n", "System: 68040\n")
    assert aros68k["os"] == "AROS" and not native_ppc(aros68k)
    os4 = describe_system("Kickstart 54.30, Workbench 53.12\n", "System: PowerPC 7447\n")
    assert os4["os"] == "AmigaOS 4" and native_ppc(os4)
    unknown = describe_system("Kickstart 53.30\n", "")
    assert unknown["os"] == "AmigaOS 4"


def test_v4_sysinfo_corrects_amigaos4_when_cpu_text_is_empty():
    """Session A capture: Version gave Kickstart 51.51 / WB 40.0, CPU text did not
    yield a 68k chip token, so describe_system chose AmigaOS 4 and skipped FPU.
    Daemon SYSINFO already had ApolloOS / 68080 / attn_flags=42111."""
    from amigent.system import describe_system, describe_fpu, native_ppc, reconcile_system

    version = "Kickstart 51.51, Workbench 40.0\n"
    mislabelled = describe_system(version, "")
    assert mislabelled["os"] == "AmigaOS 4" and native_ppc(mislabelled)
    info = {"os": "ApolloOS", "cpu": "68080", "attn_flags": "42111", "kickstart": "51.51"}
    fixed = reconcile_system(mislabelled, info)
    assert fixed["os"] == "ApolloOS" and fixed["cpu"] == "68080"
    assert not native_ppc(fixed)
    assert describe_fpu(42111, fixed["cpu"]).line() == "68080 with FPU"
    # Real AmigaOS 4 SYSINFO must not be rewritten
    os4 = describe_system("Kickstart 54.30\n", "System: PowerPC 7447\n")
    assert reconcile_system(os4, {"os": "AmigaOS 4", "cpu": "PowerPC"})["os"].startswith("AmigaOS 4")


def test_a_68080_alone_does_not_make_apolloos():
    """A Vampire with AmigaOS 3 has the 68080 bit too; only SYSINFO os=ApolloOS renames."""
    from amigent.system import describe_system, reconcile_system

    facts = describe_system("Kickstart 51.51, Workbench 40.0\n", "")
    info = {"os": "AmigaOS", "cpu": "68080", "attn_flags": "42111"}
    assert reconcile_system(facts, info) == facts
