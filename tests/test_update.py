"""amigent-setup --update: swap a running amigentd by network, judge the Amiga's verdict."""

import pytest

from amigent import setup
from amigent.client import ExecResult
from amigent.errors import AmigaError
from amigent.paths import AMIGA


class Amiga:
    """Files and the update script's outcome; the script 'runs' when rx starts it."""

    def __init__(self, cpu_rc=0, outcome=b"new", installed=b"old amigentd"):
        self.files = {"C:amigentd": installed}
        self.cpu_rc = cpu_rc
        self.outcome = outcome
        self.commands = []


def channel_for(amiga):
    class Channel:
        protocol = 4

        def __init__(self, host, timeout=30):
            pass

        def connect(self):
            pass

        def close(self):
            pass

        def read_file(self, path):
            if path not in amiga.files:
                raise AmigaError("object not found")
            return amiga.files[path]

        def write_file(self, path, data):
            amiga.files[path] = data

        def exec(self, command):
            amiga.commands.append(command)
            if command.startswith("CPU CHECK"):
                return ExecResult(amiga.cpu_rc, "")
            if command.startswith("Run >NIL: rx T:amigent-update.rexx"):
                if amiga.outcome == b"new":
                    amiga.files["C:amigentd"] = amiga.files.pop("C:amigentd.new")
                amiga.files["T:amigent-update.done"] = amiga.outcome + b"\n"
            if command.startswith("Version"):
                return ExecResult(0, "amigentd 1.2.0\n")
            return ExecResult(0, "")

    return Channel


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)

    def go(amiga, home=None):
        monkeypatch.setattr(setup, "AmigaChannel", channel_for(amiga))
        return setup.update("192.0.2.1", home)
    return go


def test_the_new_amigentd_replaces_the_old(run, capsys):
    amiga = Amiga()
    assert run(amiga, "Stuff:amigent") == 0
    assert amiga.files["C:amigentd"] == (AMIGA / "bin" / "amigentd").read_bytes()
    assert "Run >NIL: rx T:amigent-update.rexx Stuff:amigent" in amiga.commands
    assert 'Echo >ENVARC:amigent-home "Stuff:amigent"' in amiga.commands
    assert "Done: amigentd 1.2.0, protocol 4." in capsys.readouterr().out


def test_without_home_the_amiga_remembers_its_own(run):
    amiga = Amiga()
    assert run(amiga) == 0
    assert "Run >NIL: rx T:amigent-update.rexx" in amiga.commands
    assert not any("ENVARC" in c for c in amiga.commands)


def test_a_68000_gets_its_own_build(run):
    amiga = Amiga(cpu_rc=5)
    assert run(amiga) == 0
    assert amiga.files["C:amigentd"] == (AMIGA / "bin" / "amigentd.000").read_bytes()


def test_nothing_to_do_when_it_is_already_current(run, capsys):
    amiga = Amiga(installed=(AMIGA / "bin" / "amigentd").read_bytes())
    assert run(amiga) == 0
    assert "already" in capsys.readouterr().out
    assert not any("rx" in c for c in amiga.commands)


@pytest.mark.parametrize("outcome, code, words", [
    (b"old", 1, "runs the old one again"),
    (b"busy", 1, "did not stop"),
    (b"reboot", 0, "restart the Amiga"),
    (b"missing", 1, "did not arrive"),
])
def test_the_amiga_says_how_it_went(run, capsys, outcome, code, words):
    assert run(Amiga(outcome=outcome)) == code
    assert words in capsys.readouterr().out


def test_an_unknown_cpu_stops_before_anything_changes(run):
    amiga = Amiga(cpu_rc=20)
    assert run(amiga) == 1
    assert "C:amigentd.new" not in amiga.files


def test_a_home_that_cannot_be_written_does_not_stop_the_update(run, capsys):
    amiga = Amiga()
    original = channel_for(amiga)

    class Stubborn(original):
        def write_file(self, path, data):
            if path.startswith("Work:gone/"):
                raise AmigaError("object not found [205]")
            super().write_file(path, data)

    import amigent.setup as s
    s_channel = s.AmigaChannel
    try:
        s.AmigaChannel = Stubborn
        assert s.update("192.0.2.1", "Work:gone") == 0
    finally:
        s.AmigaChannel = s_channel
    assert amiga.files["C:amigentd"] == (AMIGA / "bin" / "amigentd").read_bytes()
    assert "not copied" in capsys.readouterr().out


def test_the_repair_scripts_are_renewed_for_their_folder(run, capsys):
    amiga = Amiga()
    amiga.files["Work:amigent/amigent-repair"] = b"; a repair script from the first installation\n"
    assert run(amiga, home="Work:amigent") == 0
    script = amiga.files["Work:amigent/amigent-repair"].decode("iso-8859-1")
    assert "Set SRC Work:amigent" in script and "SYS:amigent" not in script
    assert "Protect Work:amigent/amigent-repair +s" in amiga.commands
    assert "renewed in Work:amigent: amigent-repair" in capsys.readouterr().out


def test_the_scripts_wait_until_the_new_amigentd_runs(run):
    amiga = Amiga(outcome=b"old")
    amiga.files["SYS:amigent/amigent-repair"] = b"old script\n"
    assert run(amiga) == 1
    assert amiga.files["SYS:amigent/amigent-repair"] == b"old script\n"


def test_a_current_amigentd_still_gets_its_scripts_renewed(run):
    amiga = Amiga(installed=(AMIGA / "bin" / "amigentd").read_bytes())
    assert run(amiga) == 0
    assert b"Set SRC SYS:amigent" in amiga.files["SYS:amigent/amigent-repair"]


def test_a_spare_folder_repairs_from_itself_and_is_remembered(monkeypatch):
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)
    amiga = Amiga()
    monkeypatch.setattr(setup, "AmigaChannel", channel_for(amiga))
    assert setup.update("192.0.2.1", "Work:amigent", "Backup:amigent") == 0
    assert b"Set SRC Backup:amigent" in amiga.files["Backup:amigent/amigent-repair"]
    assert amiga.files["Backup:amigent/amigentd"] == (AMIGA / "bin" / "amigentd").read_bytes()
    assert 'Echo >ENVARC:amigent-spare "Backup:amigent"' in amiga.commands
    assert "Copy >NIL: Work:amigent/amigent.key Backup:amigent/ CLONE QUIET" in amiga.commands
    assert "Copy >NIL: Work:amigent/repair.d/#? Backup:amigent/repair.d/ CLONE QUIET" in amiga.commands
    # the next update finds the spare by itself
    amiga.files["ENVARC:amigent-spare"] = b"Backup:amigent\n"
    amiga.files["ENVARC:amigent-home"] = b"Work:amigent\n"
    amiga.files["Backup:amigent/amigent-repair"] = b"stale\n"
    amiga.files["C:amigentd"] = b"old amigentd"
    assert setup.update("192.0.2.1", None) == 0
    assert b"Set SRC Backup:amigent" in amiga.files["Backup:amigent/amigent-repair"]


def test_corrupt_staging_aborts_before_live_changes(monkeypatch):
    amiga = Amiga()
    original = channel_for(amiga)

    class Corrupt(original):
        def write_file(self, path, data):
            super().write_file(path, data + b"!" if path == "C:amigentd.new" else data)

    monkeypatch.setattr(setup, "AmigaChannel", Corrupt)
    assert setup.update("192.0.2.1", "Work:renamed") == 1
    assert amiga.files["C:amigentd"] == b"old amigentd"
    assert not any("rx T:" in c or "ENVARC:" in c for c in amiga.commands)


def test_candidate_must_answer_on_the_selected_address(monkeypatch):
    from types import SimpleNamespace
    candidate = b"$VER: amigentd 1.5.0 (21.11.2026)\0amigent-trial/1"
    nonce = "a" * 32
    seen = []

    class Production:
        _sock = SimpleNamespace(getpeername=lambda: ("192.0.2.1", 6801))
        def read_file(self, path):
            return candidate
        def exec(self, command, **kwargs):
            seen.append(command)
            return ExecResult(0, "amigent-trial/1 " + nonce)

    class WrongTrial:
        _sock = object()
        def __init__(self, host, port, timeout, address, trial_nonce, trial_tls):
            assert address == "192.0.2.1" and trial_nonce == nonce and port >= 49152
        def connect(self):
            pass
        def sysinfo(self):
            return {"trial": "another process"}
        def ping(self):
            pytest.fail("wrong candidate received a ping")
        def close(self):
            seen.append("closed")

    monkeypatch.setattr(setup, "AmigaChannel", WrongTrial)
    with pytest.raises(AmigaError, match="identity"):
        setup.verify_candidate(Production(), "named-machine", candidate, "C:staged", nonce)
    assert seen[-1] == "closed"
    assert not any("QUIT" in line for line in seen)


def test_recovery_copy_is_machine_scoped(private_config):
    first = setup.keep_recovery("192.0.2.1", b"original")
    second = setup.keep_recovery("192.0.2.2", b"original")
    assert first != second and first.read_bytes() == second.read_bytes() == b"original"


@pytest.mark.parametrize("flags,banner,tls", [(1, b"", False), (9, b"b" * 32, False),
                                              (9, b"a" * 32, True)])
def test_trial_collision_sends_no_key(monkeypatch, flags, banner, tls):
    import io
    import struct
    from amigent import channel

    class Peer:
        wire = io.BytesIO(b"AMGT" + struct.pack(">HH", 6, flags) + banner)
        def setsockopt(self, *args):
            pass
        def recv(self, n):
            return self.wire.read(n)
        def sendall(self, data):
            pytest.fail("sent data to an unverified candidate")
        def close(self):
            pass

    monkeypatch.setattr(channel.tls, "_dial", lambda *args: Peer())
    trial = channel.AmigaChannel("192.0.2.1", 49153, address="192.0.2.1",
                                 key="test", trial_nonce="a" * 32, trial_tls=tls)
    with pytest.raises(AmigaError, match="unexpected update trial"):
        trial.connect()
