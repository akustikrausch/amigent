import contextlib

import amigent.server as s


def restart_with(monkeypatch, answers: bool, power: bool, has_reboot: bool = True, goes_down: bool = True):
    calls = []

    def stat(self, path):
        if not has_reboot:
            raise s.AmigaError("object not found", 205)
        return {"type": "file"}

    monkeypatch.setattr(s, "ctl", type("Ctl", (), {"flush": lambda self, v: None, "stat": stat,
                                                    "exec": lambda self, c, timeout=None: None,
                                                    "close": lambda self: None})())
    monkeypatch.setattr(s, "wait_gone", lambda h, p: goes_down)
    monkeypatch.setattr(s.screen, "close", lambda: None)
    monkeypatch.setattr(s, "_exclusive", contextlib.nullcontext)
    monkeypatch.setattr(s.time, "sleep", lambda n: None)
    monkeypatch.setattr(s, "wait_for_port", lambda h, p, w: calls.append(("wait", w)) or answers)
    monkeypatch.setattr(s, "power_cycle", lambda h, p: calls.append(("cycle",)) or 1)
    monkeypatch.setattr(s, "power_available", lambda: power)
    return calls


def test_restart_cycles_power_when_the_network_stays_away(monkeypatch):
    calls = restart_with(monkeypatch, answers=False, power=True)
    assert "power was cycled" in s.amiga_restart()
    assert calls == [("wait", s.RESTART_WAIT), ("cycle",)]


def test_restart_without_power_control_waits_longer_and_reports(monkeypatch):
    calls = restart_with(monkeypatch, answers=False, power=False)
    try:
        s.amiga_restart()
        raise AssertionError("expected an error")
    except s.AmigaError as e:
        assert "amiga_power" in str(e)
    assert calls == [("wait", s.BOOT_WAIT)]


def test_restart_that_answers_needs_nothing_else(monkeypatch):
    calls = restart_with(monkeypatch, answers=True, power=True)
    assert s.amiga_restart() == "Amiga restarted"
    assert calls == [("wait", s.RESTART_WAIT)]


def test_the_lock_is_exclusive_across_threads():
    import threading
    import time

    from amigent import locks

    order = []

    def hold(tag):
        with locks.exclusive("amigent-test.lock"):
            order.append(f"{tag} in")
            time.sleep(0.2)
            order.append(f"{tag} out")

    first = threading.Thread(target=hold, args=("a",))
    first.start()
    time.sleep(0.05)
    hold("b")
    first.join()
    assert order == ["a in", "a out", "b in", "b out"]


def test_restart_without_c_reboot_says_so(monkeypatch):
    restart_with(monkeypatch, answers=True, power=True, has_reboot=False)
    try:
        s.amiga_restart()
        raise AssertionError("expected an error")
    except s.AmigaError as e:
        assert "C:Reboot" in str(e)


def test_restart_that_never_went_down_is_no_restart(monkeypatch):
    calls = restart_with(monkeypatch, answers=True, power=True, goes_down=False)
    try:
        s.amiga_restart()
        raise AssertionError("expected an error")
    except s.AmigaError as e:
        assert "still up" in str(e)
    assert calls == []
