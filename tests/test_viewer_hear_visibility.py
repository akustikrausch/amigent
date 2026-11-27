"""Stock Viewer Hear visibility — Apollo + audio=49 only (offline, not hardware)."""
from __future__ import annotations

from amigent.channel import PLAIN_CLOSED
from amigent.viewer import Viewer, apollo_machine, hear_eligible
from amigent.webserver import WEB

APOLLO_AUDIO = {
    "os": "ApolloOS",
    "cpu": "68080",
    "audio": "49",
    "daemon_protocol": 6,
}


class Page:
    """An open page: what the viewer sent it."""

    def __init__(self):
        self.got = []

    def send(self, data):
        self.got.append(data)


def _page(amiga=None, online=True):
    viewer = Viewer("192.0.2.10", screen_port=1, ctl_port=1)
    viewer.screen.flags = PLAIN_CLOSED
    viewer.page = Page()
    viewer.clients.add(viewer.page)
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
    assert "hidden" in html.split('id="hear"', 1)[1].split(">", 1)[0]
    assert "#hear[hidden]" in css
    assert "display: none !important" in css


def test_eligibility_initial_and_disconnected():
    assert not hear_eligible(None, connected=False)
    assert not hear_eligible({}, connected=True)
    assert not hear_eligible(APOLLO_AUDIO, connected=False)


def test_eligibility_connecting_requires_live_connection_flag():
    assert not hear_eligible(APOLLO_AUDIO, connected=False)


def test_eligibility_non_apollo_hidden_even_with_audio():
    info = {"os": "AmigaOS", "cpu": "68040", "audio": "49", "daemon_protocol": 6}
    assert not apollo_machine(info)
    assert not hear_eligible(info, connected=True)


def test_eligibility_protocol5_hidden():
    info = {**APOLLO_AUDIO, "daemon_protocol": 5}
    assert apollo_machine(info)
    assert not hear_eligible(info, connected=True)


def test_eligibility_apollo_audio_zero_and_legacy_1_hidden():
    assert not hear_eligible({**APOLLO_AUDIO, "audio": "0"}, connected=True)
    assert not hear_eligible({**APOLLO_AUDIO, "audio": "1"}, connected=True)


def test_eligibility_supported_apollo_audio_visible():
    assert apollo_machine(APOLLO_AUDIO)
    assert hear_eligible(APOLLO_AUDIO, connected=True)
    assert hear_eligible(
        {"os": "AmigaOS", "cpu": "68080", "audio": "49", "daemon_protocol": 6},
        connected=True,
    )


def test_protocol6_alone_is_not_apollo_evidence():
    info = {"os": "AmigaOS", "cpu": "68060", "audio": "49", "daemon_protocol": 6}
    assert not hear_eligible(info, connected=True)


def test_submit_rejects_hear_when_ineligible():
    viewer = _page(amiga={"os": "AmigaOS", "audio": "49", "daemon_protocol": 6})
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    assert viewer.hear is False
    assert ("audio", {"on": False}) in viewer.sent


def test_submit_allows_hear_when_eligible():
    viewer = _page(amiga=APOLLO_AUDIO)
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    assert viewer.hear is True and viewer._audio_ready()


def test_disconnect_clears_amiga_and_hear():
    viewer = _page(amiga=APOLLO_AUDIO)
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    assert viewer.hear is True
    viewer._set_online(False, "the Amiga does not answer")
    assert viewer.hear is False
    assert viewer.amiga == {}
    assert not hear_eligible(viewer.amiga, connected=False)


def test_retiring_the_machine_clears_capabilities():
    viewer = _page(amiga=APOLLO_AUDIO)
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    assert viewer.hear is True
    viewer.retire()
    assert viewer.hear is False and viewer.amiga == {}
    assert not hear_eligible(viewer.amiga, connected=viewer.online is True)


def test_connecting_elsewhere_does_not_move_the_saved_machine(monkeypatch):
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
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    viewer._set_online(False, "lost")
    viewer._set_online(True)
    assert viewer.amiga == {}
    assert not viewer._audio_ready()
    viewer.amiga = dict(APOLLO_AUDIO)
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    assert viewer.hear is True and viewer._audio_ready()


def test_audio_ready_stops_active_hear_when_eligibility_lost():
    viewer = _page(amiga=APOLLO_AUDIO)
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    viewer.amiga = {"os": "AmigaOS", "cpu": "68040", "audio": "49", "daemon_protocol": 6}
    assert not viewer._audio_ready()
    assert viewer.hear is False


def test_the_hear_label_is_translated_in_every_language():
    import json
    import re

    html = (WEB / "index.html").read_text(encoding="utf-8")
    button = html.split('id="hear"', 1)[1].split(">", 1)[0]
    assert 'data-i18n-label="hear"' in button and "Hear the Amiga" not in button
    script = (WEB / "ui.js").read_text(encoding="utf-8")
    codes = set(re.findall(r"\b([a-z]{2}): \"", script.split("const LANGUAGES")[1].split("};")[0]))
    english = json.loads((WEB / "locales" / "en.json").read_text(encoding="utf-8"))
    assert english["hear"] == "Hear the Amiga"
    assert codes == {p.stem for p in (WEB / "locales").glob("*.json")}
    for code in codes - {"en"}:
        texts = json.loads((WEB / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        assert texts.get("hear", "").strip() and texts["hear"] != english["hear"], code


def test_page_source_enforces_visibility_gates():
    js = (WEB / "viewer.js").read_text(encoding="utf-8")
    assert "function hearEligible(" in js
    assert "function infoHearEligible(" in js
    assert "capabilityEpoch" in js
    assert "invalidateHearCapabilities" in js
    assert "proto < 6" in js
    assert 'String(info.audio || "") === "49"' in js
    assert "apolloMachine(info)" in js
    assert "hearEligible(amiga)" in js
    assert "button.hidden = !offered" in js
    assert 'offered = !!(info && info.audio === "1")' not in js


def test_normal_activation_and_deactivation_still_work():
    viewer = _page(amiga=APOLLO_AUDIO)
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    assert viewer._hearing() and viewer._audio_ready()
    viewer.submit({"t": "audio", "on": False}, viewer.page)
    assert not viewer._hearing()
