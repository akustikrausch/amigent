"""ApolloOS identification for the installer, with the Apollo daemon in the package.

The packaged amigentd.apollo carries AUDIO; PIP is not part of it. These tests check
which packaged file the installer copies and what is in the sources. They do not run
on Apollo hardware.
"""

import hashlib
import re
import shutil
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


APOLLO_SHA = "63db83ff021c47f7146e0872cd36bf90eb201f36a0cf455a58f3c518a84e8ef1"


def test_the_apollo_daemon_is_packaged_with_the_validated_size_and_hash():
    data = (AMIGA / "bin" / "amigentd.apollo").read_bytes()
    assert len(data) == 128744 and hashlib.sha256(data).hexdigest() == APOLLO_SHA
    assert (AMIGA / "bin" / "amigent-apollo-probe").is_file()
    assert b"arne.audio" in data and b"ApolloOS" in data
    from amigent.daemons import SPECS, available_names

    assert "amigentd.apollo" in available_names(AMIGA / "bin")
    assert any(s.name == "amigentd.apollo" and s.verified for s in SPECS)


def _live_apollo_update(installed, info=None):
    """An Amiga that runs ``installed`` (ApolloOS unless ``info`` says otherwise); returns its
    state and the channel class."""
    amiga = {"C:amigentd": installed, "commands": []}
    described = info or {"os": "ApolloOS", "exec_version": "51.1", "attn_flags": "0x400"}

    class Live(Channel):
        protocol = 6

        def __init__(self, host, timeout=30):
            super().__init__(info=described)
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

    return amiga, Live


def test_update_on_apolloos_installs_the_packaged_apollo_daemon(monkeypatch):
    amiga, Live = _live_apollo_update(b"old")
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)
    monkeypatch.setattr(setup, "AmigaChannel", Live)
    assert setup.update("192.0.2.1", None) == 0
    assert amiga["C:amigentd"] == (AMIGA / "bin" / "amigentd.apollo").read_bytes()
    assert "T:amigent-apollo-probe" not in amiga


def _without_apollo_package(tmp_path, monkeypatch):
    package = tmp_path / "amiga"
    shutil.copytree(AMIGA, package)
    (package / "bin" / "amigentd.apollo").unlink()
    monkeypatch.setattr(setup, "AMIGA", package)
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)
    return package


def test_update_on_apolloos_keeps_the_daemon_there_unless_forced_when_not_packaged(
        tmp_path, monkeypatch, capsys):
    """The maintainer's policy (681e404): a package without amigentd.apollo leaves the daemon
    on an ApolloOS machine alone; --force puts the classic one on."""
    package = _without_apollo_package(tmp_path, monkeypatch)
    amiga, Live = _live_apollo_update(b"old")
    monkeypatch.setattr(setup, "AmigaChannel", Live)
    assert setup.update("192.0.2.1", None) == 0
    assert amiga["C:amigentd"] == b"old" and "C:amigentd.new" not in amiga
    assert "has no amigentd.apollo" in capsys.readouterr().out
    assert setup.update("192.0.2.1", None, force=True) == 0
    assert amiga["C:amigentd"] == (package / "bin" / "amigentd").read_bytes()


def test_update_without_the_apollo_package_never_downgrades_an_installed_apollo(
        tmp_path, monkeypatch, capsys):
    _without_apollo_package(tmp_path, monkeypatch)
    apollo = (AMIGA / "bin" / "amigentd.apollo").read_bytes()
    amiga, Live = _live_apollo_update(apollo)
    monkeypatch.setattr(setup, "AmigaChannel", Live)
    assert setup.update("192.0.2.1", None) == 0
    assert amiga["C:amigentd"] == apollo and "C:amigentd.new" not in amiga
    assert not any("amigent-update.rexx" in c for c in amiga["commands"])
    assert "Apollo" in capsys.readouterr().out


def test_force_may_put_classic_over_apollo_when_the_package_has_no_apollo(tmp_path, monkeypatch):
    package = _without_apollo_package(tmp_path, monkeypatch)
    apollo = (AMIGA / "bin" / "amigentd.apollo").read_bytes()
    amiga, Live = _live_apollo_update(apollo)
    monkeypatch.setattr(setup, "AmigaChannel", Live)
    assert setup.update("192.0.2.1", None, force=True) == 0
    assert amiga["C:amigentd"] == (package / "bin" / "amigentd").read_bytes()


def test_a_normal_update_keeps_a_recognised_apollo_when_detection_falls_back_to_classic(
        monkeypatch, capsys):
    """The package carries amigentd.apollo, but the running daemon describes a plain 68080 under
    AmigaOS, so classic is selected; the installed Apollo build is still not downgraded."""
    apollo = (AMIGA / "bin" / "amigentd.apollo").read_bytes()
    info = {"os": "AmigaOS", "exec_version": "47.4", "attn_flags": "0x400"}
    amiga, Live = _live_apollo_update(apollo, info)
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)
    monkeypatch.setattr(setup, "AmigaChannel", Live)
    assert setup.update("192.0.2.1", None) == 0
    assert amiga["C:amigentd"] == apollo and "C:amigentd.new" not in amiga
    assert "less specialised" in capsys.readouterr().out


def test_update_on_apolloos_never_selects_the_experimental_080_build():
    assert setup.select_build(0, {"os": "ApolloOS"}) == "amigentd.apollo"
    assert setup.select_build(0, {}) == "amigentd"


def test_protocol_6_ends_at_quiet_audio_is_apollo_only_and_the_probe_is_68000_only():
    daemon = (ROOT / "amiga" / "amigentd.c").read_text(encoding="utf-8")
    start = daemon.index("CMD_NOTE,")
    names = re.findall(r"CMD_[A-Z0-9]+", daemon[start:daemon.index("};", start)])
    # Opcode 48 is AUDIO on Apollo builds and EMU68 on the others: one #ifdef/#else.
    assert names == list(PROTOCOL_6) + ["CMD_AUDIO", "CMD_EMU68"]
    assert "PROTOCOL_VERSION 6" in daemon
    assert re.search(r"#ifdef AMIGENT_APOLLO\s*CMD_AUDIO = 48.*?#else\s*CMD_EMU68", daemon, re.S)
    assert "case CMD_AUDIO:" in daemon and "CMD_PIP" not in daemon
    protocol = (ROOT / "docs" / "PROTOCOL.md").read_text(encoding="utf-8")
    assert "| 48 | AUDIO |" in protocol and "Apollo build only" in protocol
    assert "| 48 | EMU68 |" in protocol

    detect = (ROOT / "amiga" / "apollo" / "apollo_detect.c").read_text(encoding="utf-8")
    probe = (ROOT / "amiga" / "apollo" / "apollo_probe.c").read_text(encoding="utf-8")
    makefile = (ROOT / "amiga" / "Makefile.apollo").read_text(encoding="utf-8")
    assert "apollo_os_here" in detect and "VREG_BOARD" in detect
    assert "CMD_AUDIO" not in detect and "CMD_PIP" not in detect
    assert "RETURN_WARN" in probe and "m68080" not in probe
    assert "amigent-apollo-probe" in makefile
    assert "$(BUILD)/amigentd.apollo" in makefile and "-DAMIGENT_APOLLO" in makefile
    assert "apollo_audio.c" in makefile and "apollo_mmio.c" in makefile
    probe_flags = makefile.split("PROBE_CFLAGS :=", 1)[1].splitlines()[0]
    assert "-m68000" in probe_flags and "-m68080" not in probe_flags  # the probe stays 68000
    assert "-m68080" in makefile and "apollo_pip" not in makefile


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
    detect = (ROOT / "amiga" / "apollo" / "apollo_detect.c").read_text(encoding="utf-8")
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
