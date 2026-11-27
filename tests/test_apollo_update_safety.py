"""An installed Apollo daemon is never silently replaced by the classic amigentd.

Every test drives the real ``setup.update``, ``consider_replacement``,
``build_for_channel`` and ``select_build`` against a stand-in package and a fake
Amiga. Nothing here contacts hardware.
"""

import shutil
from pathlib import Path

import pytest

from amigent import daemons, setup
from amigent.client import ExecResult
from amigent.daemons import (
    CPU_68080, Machine, available_names, choose, consider_replacement, identify, is_apollo_daemon,
)
from amigent.errors import AmigaError
from amigent.paths import AMIGA

HUNK = b"\x00\x00\x03\xf3"
APOLLO_MARK = b"tracepoints are not supported on this Apollo build yet"
APOLLO_INFO = {"os": "ApolloOS", "exec_version": "51.1", "attn_flags": "0x400"}
VAMPIRE_INFO = {"os": "AmigaOS", "exec_version": "47.4", "attn_flags": "0x400"}
EXEC51_INFO = {"exec_version": "51.1", "attn_flags": "0x8f"}  # AROS-like: no 68080 bit
HOME = "SYS:amigent"


def build(version="1.6.0", date="24.11.2026", tag=b"classic", apollo=False):
    data = HUNK + f"$VER: amigentd {version} ({date})".encode() + b"\x00" + tag
    return data + (b"\x00" + APOLLO_MARK if apollo else b"")


CLASSIC = build()
CLASSIC_000 = build(tag=b"classic-000")
APOLLO = build(tag=b"apollo", apollo=True)


def make_package(tmp_path, monkeypatch, *, apollo=APOLLO, classic=CLASSIC, probe=True):
    """A copy of the package's scripts with stand-in daemons; ``apollo=None`` leaves it out."""
    package = tmp_path / "amiga"
    shutil.copytree(AMIGA, package)
    bin_dir = package / "bin"
    for name in ("amigentd", "amigentd.000", "amigentd.080", "amigentd.apollo", "amigent-apollo-probe"):
        (bin_dir / name).unlink(missing_ok=True)
    if classic is not None:
        (bin_dir / "amigentd").write_bytes(classic)
        (bin_dir / "amigentd.000").write_bytes(CLASSIC_000)
    (bin_dir / "amigentd.080").write_bytes(build(version="1.4.0", tag=b"080"))  # historical
    if apollo is not None:
        (bin_dir / "amigentd.apollo").write_bytes(apollo)
    if probe:
        (bin_dir / "amigent-apollo-probe").write_bytes(b"probe")
    monkeypatch.setattr(setup, "AMIGA", package)
    return bin_dir


class FakeAmiga:
    """One Amiga that runs amigentd: files, a CPU answer, what the daemon says about itself."""

    protocol = 6

    def __init__(self, installed, *, cpu_rc=0, info=None, version="", probe="AmigaOS\n",
                 read_error=None):
        self.files = {} if installed is None else {"C:amigentd": installed}
        self.cpu_rc, self.info, self.version, self.probe = cpu_rc, info, version, probe
        self.read_error = read_error
        self.commands, self.written = [], []

    def connect(self):
        pass

    def close(self):
        pass

    def sysinfo(self):
        if self.info is None:
            raise AttributeError("no sysinfo")
        return self.info

    def read_file(self, path):
        if self.read_error and path == "C:amigentd":
            raise self.read_error
        if path not in self.files:
            raise AmigaError("object not found", code=205)
        return self.files[path]

    def write_file(self, path, data):
        self.written.append(path)
        self.files[path] = data

    def exec(self, command):
        self.commands.append(command)
        if command.startswith("CPU"):
            return ExecResult(self.cpu_rc, "")
        if command.startswith("Version FULL"):
            return ExecResult(0, self.version)
        if command.startswith("T:amigent-apollo-probe"):
            return ExecResult(0, self.probe)
        if command.startswith("Version"):
            return ExecResult(0, "amigentd\n")
        if command.startswith("Run >NIL: rx T:amigent-update.rexx"):
            self.files["C:amigentd"] = self.files.pop("C:amigentd.new")
            self.files["T:amigent-update.done"] = b"new\n"
        return ExecResult(0, "")

    @property
    def replaced(self):
        return "C:amigentd.new" in self.written


def run_update(monkeypatch, amiga, *, force=False):
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)
    monkeypatch.setattr(setup, "AmigaChannel", lambda host, timeout=30: amiga)
    return setup.update("192.0.2.1", None, force=force)


def scripts_renewed(amiga):
    return all(f"{HOME}/{name}" in amiga.files for name in setup.SCRIPTS)


def test_verified_apolloos_with_local_apollo_build_selects_apollo(tmp_path, monkeypatch):
    make_package(tmp_path, monkeypatch)
    assert setup.select_build(0, APOLLO_INFO) == "amigentd.apollo"
    assert setup.build_for_channel(FakeAmiga(b"x", info=APOLLO_INFO)) == "amigentd.apollo"
    assert choose(Machine(CPU_68080, apollo=True), available_names(setup.AMIGA / "bin")).name == "amigentd.apollo"


def test_apolloos_without_the_apollo_package_selects_classic_for_a_fresh_install(tmp_path, monkeypatch):
    make_package(tmp_path, monkeypatch, apollo=None)
    assert setup.select_build(0, APOLLO_INFO) == "amigentd"
    assert setup.build_for_channel(FakeAmiga(b"x", info=APOLLO_INFO)) == "amigentd"


@pytest.mark.parametrize("cpu_rc,expected", [(5, "amigentd.000"), (0, "amigentd")])
def test_classic_cpus_keep_their_existing_selection(tmp_path, monkeypatch, cpu_rc, expected):
    make_package(tmp_path, monkeypatch)
    assert setup.select_build(cpu_rc, VAMPIRE_INFO if cpu_rc == 0 else None) == expected
    assert setup.build_for_channel(FakeAmiga(b"x", cpu_rc=cpu_rc, info=VAMPIRE_INFO)) == expected
    present = available_names(setup.AMIGA / "bin")
    for cpu in (68000, 68010):
        assert choose(Machine(cpu), present).name == "amigentd.000"
    for cpu in (68020, 68030, 68040, 68060):
        assert choose(Machine(cpu), present).name == "amigentd"


def test_a_vampire_on_amigaos_never_gets_080_or_apollo_automatically(tmp_path, monkeypatch):
    make_package(tmp_path, monkeypatch)
    present = available_names(setup.AMIGA / "bin")
    assert "amigentd.080" in present
    assert choose(Machine(CPU_68080), present).name == "amigentd"
    assert setup.build_for_channel(FakeAmiga(b"x", info=VAMPIRE_INFO)) == "amigentd"
    assert setup.select_build(0, VAMPIRE_INFO) == "amigentd"


def test_exec_51_without_apolloos_is_not_taken_for_apollo(tmp_path, monkeypatch):
    make_package(tmp_path, monkeypatch)
    assert setup.select_build(0, EXEC51_INFO) == "amigentd"
    assert setup.build_for_channel(FakeAmiga(b"x", info=EXEC51_INFO)) == "amigentd"
    assert setup.build_for_channel(FakeAmiga(b"x", info={}, version="AROS exec 51.1")) == "amigentd"


def test_an_unknown_cpu_is_explained_and_never_reaches_apollo(tmp_path, monkeypatch, capsys):
    make_package(tmp_path, monkeypatch)

    class Link:
        puts = []

        def run(self, command):
            return 20

        def put(self, name, data):
            self.puts.append(name)

    assert setup.amigentd_build(Link()) == "amigentd.000"
    assert "68000 build" in capsys.readouterr().out and Link.puts == []
    with pytest.raises(SystemExit):
        setup.select_build(20, APOLLO_INFO)
    with pytest.raises(SystemExit) as stopped:
        setup.build_for_channel(FakeAmiga(APOLLO, cpu_rc=20, info=APOLLO_INFO))
    assert "left in place" in str(stopped.value)


def test_an_update_with_an_unknown_cpu_leaves_an_installed_apollo_alone(tmp_path, monkeypatch, capsys):
    make_package(tmp_path, monkeypatch)
    amiga = FakeAmiga(APOLLO, cpu_rc=20, info=APOLLO_INFO)
    assert run_update(monkeypatch, amiga) == 1
    assert amiga.files["C:amigentd"] == APOLLO and not amiga.replaced
    assert "left in place" in capsys.readouterr().out


def test_an_apollo_daemon_is_recognised_by_its_markers_alone(tmp_path):
    assert is_apollo_daemon(APOLLO)
    assert not is_apollo_daemon((AMIGA / "bin" / "amigentd").read_bytes())
    assert not is_apollo_daemon((AMIGA / "bin" / "amigentd.000").read_bytes())
    assert not is_apollo_daemon(CLASSIC) and not is_apollo_daemon(b"")
    assert not is_apollo_daemon(b"not an executable " + APOLLO_MARK)  # needs the HUNK header
    empty = tmp_path / "bin"
    empty.mkdir()
    assert identify(APOLLO, empty) is None  # not packaged, still recognised by is_apollo_daemon


def test_no_digest_of_a_particular_binary_decides_an_update():
    import re

    source = Path(daemons.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\b[0-9a-f]{64}\b", source) and "hashlib" not in source
    assert not any(name.endswith("SHA256") for name in vars(daemons))


def test_an_installed_apollo_of_any_build_is_updated_by_a_newer_packaged_apollo(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "amigentd.apollo").write_bytes(APOLLO)
    for tag in (b"fork-a", b"fork-b", b"own-build"):
        installed = build(version="1.5.0", date="18.11.2026", tag=tag, apollo=True)
        assert consider_replacement(installed, APOLLO, "amigentd.apollo", bin_dir) == (True, "")
        keep, reason = consider_replacement(installed, CLASSIC, "amigentd", bin_dir)
        assert not keep and "less specialised" in reason


def test_a_classic_candidate_never_replaces_an_apollo_daemon_even_when_newer(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    newer_classic = build(version="1.9.0", date="1.1.2030")
    old_apollo = build(version="1.2.0", date="1.1.2020", tag=b"old", apollo=True)
    keep, reason = consider_replacement(old_apollo, newer_classic, "amigentd", bin_dir)
    assert not keep and "Apollo" in reason and "less specialised" in reason
    keep, _ = consider_replacement(old_apollo, CLASSIC_000, "amigentd.000", bin_dir)
    assert not keep


def test_apollo_candidates_replace_older_apollo_and_do_not_downgrade(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "amigentd.apollo").write_bytes(build(version="1.6.0", date="24.11.2026", tag=b"mid", apollo=True))
    candidate = (bin_dir / "amigentd.apollo").read_bytes()
    older = build(version="1.5.0", date="18.11.2026", tag=b"old", apollo=True)
    newer = build(version="1.7.0", date="1.1.2027", tag=b"new", apollo=True)
    same_version_later = build(version="1.6.0", date="25.11.2026", tag=b"later", apollo=True)
    same_version_earlier = build(version="1.6.0", date="20.11.2026", tag=b"earlier", apollo=True)
    assert consider_replacement(older, candidate, "amigentd.apollo", bin_dir) == (True, "")
    assert consider_replacement(same_version_earlier, candidate, "amigentd.apollo", bin_dir) == (True, "")
    keep, reason = consider_replacement(newer, candidate, "amigentd.apollo", bin_dir)
    assert not keep and "newer" in reason
    keep, reason = consider_replacement(same_version_later, candidate, "amigentd.apollo", bin_dir)
    assert not keep and "later date" in reason
    keep, reason = consider_replacement(candidate, candidate, "amigentd.apollo", bin_dir)
    assert not keep and "same executable" in reason


def test_an_older_classic_is_still_replaced_by_the_apollo_candidate(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "amigentd").write_bytes(CLASSIC)
    replace, reason = consider_replacement(build(version="1.3.0", date="1.1.2020"), APOLLO,
                                           "amigentd.apollo", bin_dir)
    assert replace and reason == ""


def test_unknown_installed_daemons_are_kept_unless_clearly_older(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "amigentd").write_bytes(CLASSIC)
    same = build(version="1.6.0", date="24.11.2026", tag=b"custom")
    newer = build(version="1.7.0", date="1.1.2027", tag=b"custom")
    older = build(version="1.3.0", date="1.1.2020", tag=b"custom")
    keep, reason = consider_replacement(same, CLASSIC, "amigentd", bin_dir)
    assert not keep and "same version" in reason
    keep, reason = consider_replacement(newer, CLASSIC, "amigentd", bin_dir)
    assert not keep and "newer" in reason
    assert consider_replacement(older, CLASSIC, "amigentd", bin_dir) == (True, "")
    keep, reason = consider_replacement(older + b"\x00" + APOLLO_MARK, CLASSIC, "amigentd", bin_dir)
    assert not keep and "Apollo" in reason


def test_update_installs_apollo_over_an_older_classic_when_apollo_is_staged(tmp_path, monkeypatch):
    make_package(tmp_path, monkeypatch)
    amiga = FakeAmiga(build(version="1.3.0", date="1.1.2020"), info=APOLLO_INFO)
    assert run_update(monkeypatch, amiga) == 0
    assert amiga.files["C:amigentd"] == APOLLO and scripts_renewed(amiga)


def test_update_replaces_an_older_apollo_with_a_newer_eligible_apollo(tmp_path, monkeypatch):
    make_package(tmp_path, monkeypatch, apollo=build(version="1.7.0", date="1.2.2027", tag=b"new", apollo=True))
    amiga = FakeAmiga(build(version="1.5.0", tag=b"old", apollo=True), info=APOLLO_INFO)
    assert run_update(monkeypatch, amiga) == 0
    assert amiga.replaced and amiga.files["C:amigentd"] != APOLLO
    assert b"new" in amiga.files["C:amigentd"] and is_apollo_daemon(amiga.files["C:amigentd"])


def test_update_does_not_downgrade_an_apollo_to_an_older_apollo(tmp_path, monkeypatch, capsys):
    make_package(tmp_path, monkeypatch)
    installed = build(version="1.7.0", date="1.1.2027", tag=b"newer", apollo=True)
    amiga = FakeAmiga(installed, info=APOLLO_INFO)
    assert run_update(monkeypatch, amiga) == 0
    assert amiga.files["C:amigentd"] == installed and not amiga.replaced
    assert "newer" in capsys.readouterr().out and scripts_renewed(amiga)


def test_apollo_running_and_the_apollo_package_missing_keeps_the_apollo(tmp_path, monkeypatch, capsys):
    make_package(tmp_path, monkeypatch, apollo=None)
    amiga = FakeAmiga(APOLLO, info=APOLLO_INFO)
    assert run_update(monkeypatch, amiga) == 0
    assert amiga.files["C:amigentd"] == APOLLO and not amiga.replaced
    assert scripts_renewed(amiga)
    out = capsys.readouterr().out
    assert "Apollo" in out and "no amigentd.apollo" in out


def test_apollo_running_with_a_classic_selection_is_preserved(tmp_path, monkeypatch, capsys):
    make_package(tmp_path, monkeypatch)
    amiga = FakeAmiga(APOLLO, info=VAMPIRE_INFO)
    assert setup.build_for_channel(amiga) == "amigentd"
    assert run_update(monkeypatch, amiga) == 0
    assert amiga.files["C:amigentd"] == APOLLO and not amiga.replaced
    assert "less specialised" in capsys.readouterr().out and scripts_renewed(amiga)


def test_a_probe_that_fails_does_not_downgrade_an_installed_apollo(tmp_path, monkeypatch):
    make_package(tmp_path, monkeypatch)
    amiga = FakeAmiga(APOLLO, info={}, probe="AmigaOS\n")
    assert run_update(monkeypatch, amiga) == 0
    assert amiga.files["C:amigentd"] == APOLLO and not amiga.replaced


def test_an_identical_candidate_keeps_the_binary_and_refreshes_the_scripts(tmp_path, monkeypatch, capsys):
    make_package(tmp_path, monkeypatch)
    amiga = FakeAmiga(APOLLO, info=APOLLO_INFO)
    assert run_update(monkeypatch, amiga) == 0
    assert "C:amigentd.new" not in amiga.written and amiga.files["C:amigentd"] == APOLLO
    assert scripts_renewed(amiga)
    assert "already the one" in capsys.readouterr().out


def test_unchanged_daemon_still_renews_scripts_and_the_home_folder_note(tmp_path, monkeypatch):
    make_package(tmp_path, monkeypatch, apollo=None)
    amiga = FakeAmiga(CLASSIC, cpu_rc=0, info=VAMPIRE_INFO)
    stale = {f"{HOME}/{name}": b"stale" for name in setup.SCRIPTS}
    amiga.files.update(stale)
    assert run_update(monkeypatch, amiga) == 0
    assert not amiga.replaced
    for name in setup.SCRIPTS:
        assert amiga.files[f"{HOME}/{name}"] != b"stale"
    assert f"Protect {HOME}/amigent-repair +s" in amiga.commands


def test_force_puts_classic_on_when_the_package_has_no_apollo(tmp_path, monkeypatch):
    make_package(tmp_path, monkeypatch, apollo=None)
    amiga = FakeAmiga(APOLLO, info=APOLLO_INFO)
    assert run_update(monkeypatch, amiga) == 0
    assert amiga.files["C:amigentd"] == APOLLO and not amiga.replaced
    amiga = FakeAmiga(APOLLO, info=APOLLO_INFO)
    assert run_update(monkeypatch, amiga, force=True) == 0
    assert amiga.files["C:amigentd"] == CLASSIC and amiga.replaced


def test_a_dead_connection_is_not_a_missing_install(tmp_path, monkeypatch, capsys):
    make_package(tmp_path, monkeypatch)
    amiga = FakeAmiga(APOLLO, info=APOLLO_INFO, read_error=AmigaError("connection closed by amigentd"))
    assert run_update(monkeypatch, amiga) == 1
    assert not amiga.replaced and not scripts_renewed(amiga)
    assert "connection closed" in capsys.readouterr().out


def test_a_missing_install_is_reported_and_nothing_is_replaced(tmp_path, monkeypatch, capsys):
    make_package(tmp_path, monkeypatch)
    amiga = FakeAmiga(None, info=APOLLO_INFO)
    assert run_update(monkeypatch, amiga) == 0
    assert not amiga.replaced and "no C:amigentd" in capsys.readouterr().out
    assert scripts_renewed(amiga)
    forced = FakeAmiga(None, info=APOLLO_INFO)
    assert run_update(monkeypatch, forced, force=True) == 0 and forced.files["C:amigentd"] == APOLLO
