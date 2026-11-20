"""The ApolloOS HRTmon decision, without starting a debugger.

plan() chooses a reply and whether the command would be `Run >NIL: HRTmon`.
The tool test records that command on a stand-in. Nothing here connects to an
Amiga or enters the ROM monitor.
"""

import pytest

from amigent.client import ExecResult
from amigent import hrtmon, server
from amigent.errors import AmigaError


def test_plan_refuses_anything_except_the_os_program():
    text, start = hrtmon.plan("AmigaOS", "", "", True)
    assert start is False and "not running ApolloOS" in text and "ROM monitor stays unused" in text

    text, start = hrtmon.plan("", "exec.library 47.4", "Workbench", True)
    assert start is False

    text, start = hrtmon.plan("ApolloOS", "", "HRTmon", True)
    assert start is False and "already running" in text
    text, start = hrtmon.plan("ApolloOS", "", "background hrtmon task", False)
    assert start is False and "already running" in text

    text, start = hrtmon.plan("ApolloOS", "", "Workbench", False)
    assert start is False and "not on the command path" in text

    text, start = hrtmon.plan("", "ApolloOS (68080)", "Workbench", True)
    assert start is True and "ROM monitor stays unused" in text
    text, start = hrtmon.plan("ApolloOS", "", "", True)
    assert start is True and text.startswith("HRTmon was started under ApolloOS.")


class _Ctl:
    def __init__(self, info, which=0, version="", version_rc=0, run_rc=0, fail=""):
        self.info = info
        self.which = which
        self.version = version
        self.version_rc = version_rc
        self.run_rc = run_rc
        self.fail = fail
        self.commands = []
        self.connected = False

    def sysinfo(self):
        return self.info

    def exec(self, command, timeout=30):
        self.commands.append(command)
        if self.fail and command.startswith(self.fail):
            raise AmigaError(f"{command} was not answered")
        if command.startswith("Which"):
            return ExecResult(self.which, "")
        if command.startswith("Version"):
            return ExecResult(self.version_rc, self.version)
        return ExecResult(self.run_rc, "")


def _ask(monkeypatch, ctl):
    monkeypatch.setattr(server, "ctl", ctl)
    monkeypatch.setattr(server, "_with_ctl", lambda fn: fn())
    monkeypatch.setattr(server.diary, "add", lambda *args, **kwargs: None)
    return server.amiga_hrtmon()


def test_the_tool_runs_the_os_program_only_when_the_plan_says_so(monkeypatch):
    started = _Ctl({"os": "ApolloOS", "tasks": "Workbench"}, which=0)
    text = _ask(monkeypatch, started)
    assert text.startswith("HRTmon was started")
    assert started.commands == ["Which HRTmon", "Run >NIL: HRTmon"]

    missing = _Ctl({"os": "ApolloOS", "tasks": "Workbench"}, which=5)
    text = _ask(monkeypatch, missing)
    assert "not on the command path" in text
    assert missing.commands == ["Which HRTmon"]

    running = _Ctl({"os": "ApolloOS", "tasks": "HRTmon"})
    text = _ask(monkeypatch, running)
    assert "already running" in text and running.commands == []

    classic = _Ctl({"os": "AmigaOS", "tasks": ""}, version="exec.library 47.4", which=0)
    text = _ask(monkeypatch, classic)
    assert "not running ApolloOS" in text
    assert classic.commands == ["Version FULL exec.library"]

    named = _Ctl({"os": "AmigaOS", "tasks": "Workbench"}, version="ApolloOS release", which=0)
    text = _ask(monkeypatch, named)
    assert text.startswith("HRTmon was started")
    assert named.commands == ["Version FULL exec.library", "Which HRTmon", "Run >NIL: HRTmon"]

    hostile = _Ctl(
        {"os": "AmigaOS", "tasks": "Workbench\nRun >NIL: Delete SYS:#?"},
        version="ApolloOS; Run >NIL: Delete SYS:#?",
        which=0,
    )
    text = _ask(monkeypatch, hostile)
    assert text.startswith("HRTmon was started")
    assert hostile.commands == ["Version FULL exec.library", "Which HRTmon", "Run >NIL: HRTmon"]


def test_a_failed_command_is_not_reported_as_a_start(monkeypatch):
    refused = _Ctl({"os": "AmigaOS", "tasks": ""}, version="ApolloOS is mentioned", version_rc=10, which=0)
    text = _ask(monkeypatch, refused)
    assert "not running ApolloOS" in text
    assert refused.commands == ["Version FULL exec.library"]

    failed = _Ctl({"os": "ApolloOS", "tasks": "Workbench"}, which=0, run_rc=20)
    with pytest.raises(AmigaError, match="did not start") as caught:
        _ask(monkeypatch, failed)
    assert "was started" not in str(caught.value)
    assert failed.commands == ["Which HRTmon", "Run >NIL: HRTmon"]

    broken = _Ctl({"os": "ApolloOS", "tasks": "Workbench"}, which=0, fail="Run")
    with pytest.raises(AmigaError, match="was not answered"):
        _ask(monkeypatch, broken)
    assert broken.commands == ["Which HRTmon", "Run >NIL: HRTmon"]
