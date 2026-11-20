"""amigent-doctor --help is a successful request. A bad command line is not."""

import sys

from amigent import doctor
from amigent.amigentd import AmigentdError


def test_help_prints_usage_and_exits_zero(monkeypatch, capsys):
    for flag in ("--help", "-h"):
        monkeypatch.setattr(sys, "argv", ["amigent-doctor", flag])
        assert doctor.main() == 0
        out = capsys.readouterr().out
        assert "Usage:" in out
        assert "python -m amigent.doctor" in out


def test_too_many_arguments_still_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["amigent-doctor", "one", "two"])
    assert doctor.main() == 2
    assert "Usage:" in capsys.readouterr().out


def test_an_unknown_amiga_still_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["amigent-doctor"])
    monkeypatch.setattr(doctor.machines, "pick", lambda host: "")
    assert doctor.main() == 2
    assert "No Amiga known yet" in capsys.readouterr().out


def test_a_failed_diagnosis_still_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["amigent-doctor", "10.0.0.1"])
    monkeypatch.setattr(doctor.machines, "pick", lambda host: host)
    monkeypatch.setattr(doctor.machines, "find", lambda host: None)

    def unreachable(self):
        raise OSError("unreachable")

    def screen_down(self):
        raise AmigentdError("down")

    monkeypatch.setattr(doctor.Commands, "connect", unreachable)
    monkeypatch.setattr(doctor.Amigentd, "connect", screen_down)
    monkeypatch.setattr(doctor, "check_14", lambda ctl, entry: False)
    assert doctor.main() == 1
    out = capsys.readouterr().out
    assert "commands   FAIL" in out
    assert "amigentd   FAIL" in out
