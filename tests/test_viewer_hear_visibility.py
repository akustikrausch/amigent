"""Stock Viewer Hear visibility — Apollo + audio=1 only (offline, not hardware)."""
from __future__ import annotations

from pathlib import Path

import pytest

from amigent.channel import PLAIN_CLOSED
from amigent.viewer import Viewer, apollo_machine, hear_eligible
from amigent.webserver import WEB

APOLLO_AUDIO = {
    "os": "ApolloOS",
    "cpu": "68080",
    "audio": "1",
    "daemon_protocol": 6,
}


def _page(amiga=None, online=True):
    viewer = Viewer("192.0.2.10", screen_port=1, ctl_port=1)
    viewer.screen.flags = PLAIN_CLOSED
    viewer.clients.add(object())
    viewer.online = online
    viewer.sent = []
    viewer.broadcast = lambda data: viewer.sent.append(data)
    viewer.event = lambda kind, **fields: viewer.sent.append((kind, fields))
    viewer.amiga = dict(amiga) if amiga is not None else {}
    return viewer


def test_hear_button_hidden_in_initial_html_and_css():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    css = (WEB / "viewer.css").read_text(encoding="utf-8")
    assert 'id="hear"' in html
    assert 'hidden' in html.split('id="hear"', 1)[1].split(">", 1)[0]
    assert "#hear[hidden]" in css
    assert "display: none !important" in css


def test_eligibility_initial_and_disconnected():
    assert not hear_eligible(None, connected=False)
    assert not hear_eligible({}, connected=True)
    assert not hear_eligible(APOLLO_AUDIO, connected=False)


def test_eligibility_connecting_requires_live_connection_flag():
    # Page-side amigaOnline null/false maps to connected=False here.
    assert not hear_eligible(APOLLO_AUDIO, connected=False)


def test_eligibility_non_apollo_hidden_even_with_audio():
    info = {"os": "AmigaOS", "cpu": "68040", "audio": "1", "daemon_protocol": 6}
    assert not apollo_machine(info)
    assert not hear_eligible(info, connected=True)


def test_eligibility_protocol5_hidden():
    info = {**APOLLO_AUDIO, "daemon_protocol": 5}
    assert apollo_machine(info)
    assert not hear_eligible(info, connected=True)


def test_eligibility_apollo_audio_zero_hidden():
    info = {**APOLLO_AUDIO, "audio": "0"}
    assert not hear_eligible(info, connected=True)


def test_eligibility_supported_apollo_audio_visible():
    assert apollo_machine(APOLLO_AUDIO)
    assert hear_eligible(APOLLO_AUDIO, connected=True)
    # 68080 without ApolloOS IdString still counts as Apollo machine identity.
    assert hear_eligible(
        {"os": "AmigaOS", "cpu": "68080", "audio": "1", "daemon_protocol": 6},
        connected=True,
    )


def test_protocol6_alone_is_not_apollo_evidence():
    info = {"os": "AmigaOS", "cpu": "68060", "audio": "1", "daemon_protocol": 6}
    assert not hear_eligible(info, connected=True)


def test_submit_rejects_hear_when_ineligible():
    viewer = _page(amiga={"os": "AmigaOS", "audio": "1", "daemon_protocol": 6})
    viewer.submit({"t": "audio", "on": True})
    assert viewer.hear is False
    assert ("audio", {"on": False}) in viewer.sent


def test_submit_allows_hear_when_eligible():
    viewer = _page(amiga=APOLLO_AUDIO)
    viewer.submit({"t": "audio", "on": True})
    assert viewer.hear is True and viewer._audio_ready()


def test_disconnect_clears_amiga_and_hear():
    viewer = _page(amiga=APOLLO_AUDIO)
    viewer.submit({"t": "audio", "on": True})
    assert viewer.hear is True
    viewer._set_online(False, "the Amiga does not answer")
    assert viewer.hear is False
    assert viewer.amiga == {}
    assert not hear_eligible(viewer.amiga, connected=False)


def test_retiring_the_machine_clears_capabilities(monkeypatch):
    """1.5.0: an address edit or removal retires the viewer; Hear must not outlive it."""
    viewer = _page(amiga=APOLLO_AUDIO)
    viewer.submit({"t": "audio", "on": True})
    assert viewer.hear is True
    viewer.retire()
    assert viewer.hear is False and viewer.amiga == {}
    assert not hear_eligible(viewer.amiga, connected=viewer.online is True)


def test_connecting_elsewhere_does_not_move_the_saved_machine(monkeypatch):
    """1.5.0: set_address hands over to the machine manager and never rewrites this machine."""
    viewer = _page(amiga=APOLLO_AUDIO)
    monkeypatch.setattr("amigent.viewer.machines.move",
                        lambda *a: (_ for _ in ()).throw(AssertionError("moved")))
    asked = []

    class Manager:
        def connect(self, new):
            asked.append(new)
            return "other"

    monkeypatch.setattr(viewer, "machine_manager", lambda: Manager())
    assert viewer.set_address("192.0.2.20") == "other"
    assert asked == ["192.0.2.20"]


def test_reconnect_requires_fresh_apollo_capabilities():
    viewer = _page(amiga=APOLLO_AUDIO)
    viewer.submit({"t": "audio", "on": True})
    viewer._set_online(False, "lost")
    viewer._set_online(True)
    assert viewer.amiga == {}
    assert not viewer._audio_ready()
    viewer.amiga = dict(APOLLO_AUDIO)
    viewer.submit({"t": "audio", "on": True})
    assert viewer.hear is True and viewer._audio_ready()


def test_audio_ready_stops_active_hear_when_eligibility_lost():
    viewer = _page(amiga=APOLLO_AUDIO)
    viewer.submit({"t": "audio", "on": True})
    viewer.amiga = {"os": "AmigaOS", "cpu": "68040", "audio": "1", "daemon_protocol": 6}
    assert not viewer._audio_ready()
    assert viewer.hear is False


def test_page_source_enforces_visibility_gates():
    js = (WEB / "viewer.js").read_text(encoding="utf-8")
    assert "function hearEligible(" in js
    assert "function infoHearEligible(" in js
    assert "capabilityEpoch" in js
    assert "invalidateHearCapabilities" in js
    assert "proto < 6" in js
    assert 'info.audio === "1"' in js
    assert "apolloMachine(info)" in js
    assert "hearEligible(amiga)" in js
    # Must not offer Hear from audio=1 alone.
    assert "button.hidden = !offered" in js
    assert 'offered = !!(info && info.audio === "1")' not in js


def test_normal_activation_and_deactivation_still_work():
    viewer = _page(amiga=APOLLO_AUDIO)
    viewer.submit({"t": "audio", "on": True})
    assert viewer._hearing() and viewer._audio_ready()
    viewer.submit({"t": "audio", "on": False})
    assert not viewer._hearing()
