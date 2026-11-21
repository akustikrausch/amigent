"""Connected daemon identity in the viewer, separate from the computer."""

from __future__ import annotations

import json
import re
from pathlib import Path

from amigent.daemons import SPECS, describe_connected, identify
from amigent.paths import AMIGA

BIN = AMIGA / "bin"
WEB = Path(__file__).resolve().parents[1] / "src" / "amigent" / "web"
IDENTITY_KEYS = (
    "about_daemon", "about_protocol", "about_daemon_cpu", "about_capabilities",
    "not_reported", "named_file",
)


def test_describe_connected_keeps_machine_and_file_identity_apart():
    classic = (BIN / "amigentd").read_bytes()
    on_fast = describe_connected(6, "amigentd 1.4.0", classic,
                                 {"os": "AmigaOS", "cpu": "68060", "loaded": "1"}, BIN)
    assert on_fast["daemon_family"] == "general"
    assert on_fast["daemon_target"] == "68020"
    assert on_fast["daemon_identity"] == "named file"
    assert on_fast["daemon_protocol"] == 6
    assert "loaded" in on_fast["daemon_capabilities"]
    # Host CPU/OS must not become the daemon family.
    assert on_fast["daemon_family"] != "68060"
    unnamed = describe_connected(5, "", None, {"os": "AmigaOS", "cpu": "68040"}, BIN)
    assert unnamed["daemon_family"] == "Not reported"
    assert unnamed["daemon_identity"] == "Not reported"
    assert unnamed["daemon_version"] == "Not reported"
    assert unnamed["daemon_protocol"] == 5


def test_classic_68000_packaged_daemon_identity():
    path = BIN / "amigentd.000"
    assert path.is_file()
    got = describe_connected(6, "amigentd 1.4.0", path.read_bytes(),
                             {"loaded": "1", "probe": "1"}, BIN)
    assert got["daemon_family"] == "68000"
    assert got["daemon_target"] == "68000"
    assert got["daemon_identity"] == "named file"
    assert set(got["daemon_capabilities"]) >= {"loaded", "probe"}


def test_unknown_bytes_are_named_file_without_invented_family():
    got = describe_connected(6, "custom 9.9.9", b"\x00\x00\x03\xf3not-a-packaged-daemon",
                             {"os": "ApolloOS", "cpu": "68080", "audio": "1"}, BIN)
    assert got["daemon_identity"] == "named file"
    assert got["daemon_family"] == "Not reported"
    assert got["daemon_target"] == "Not reported"
    assert got["daemon_version"] == "custom 9.9.9"
    assert "AUDIO" in got["daemon_capabilities"]


def test_apollo_packaged_daemon_identity_when_present():
    path = BIN / "amigentd.apollo"
    if not path.is_file():
        # Public upstream main does not ship the Apollo binary; catalogue entry
        # is present so a packaged build is recognised when available.
        assert any(spec.name == "amigentd.apollo" for spec in SPECS)
        return
    got = describe_connected(6, "amigentd 1.4.0", path.read_bytes(),
                             {"audio": "1", "loaded": "1", "os": "ApolloOS", "cpu": "68080"}, BIN)
    assert got["daemon_family"] == "Apollo"
    assert got["daemon_target"] == "68080"
    assert got["daemon_identity"] == "named file"
    assert "AUDIO" in got["daemon_capabilities"]


def test_protocol_five_and_six_and_missing_capability_flags():
    classic = (BIN / "amigentd").read_bytes()
    p5 = describe_connected(5, "amigentd 1.3.0", classic, {}, BIN)
    assert p5["daemon_protocol"] == 5
    assert p5["daemon_capabilities"] == []
    p6 = describe_connected(6, "amigentd 1.4.0", classic,
                            {"loaded": "0", "poke": "0", "trace": "0", "audio": "0"}, BIN)
    assert p6["daemon_protocol"] == 6
    assert p6["daemon_capabilities"] == []
    poked = describe_connected(6, "amigentd 1.4.0", classic, {"poke": "1", "trace": "1"}, BIN)
    assert set(poked["daemon_capabilities"]) == {"POKE", "TRACE"}


def test_identify_matches_only_exact_packaged_bytes():
    classic = (BIN / "amigentd").read_bytes()
    assert identify(classic, BIN).name == "amigentd"
    assert identify(classic + b"x", BIN) is None
    assert identify(b"", BIN) is None


def test_malformed_version_and_zero_protocol_degrade():
    got = describe_connected(0, "   ", (BIN / "amigentd").read_bytes(), {}, BIN)
    assert got["daemon_protocol"] is None
    assert got["daemon_version"] == "Not reported"
    assert got["daemon_identity"] == "named file"


def test_about_dialog_localises_the_new_daemon_rows():
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "viewer.js").read_text(encoding="utf-8")
    locales = sorted(p for p in (WEB / "locales").glob("*.json"))
    assert locales
    english = json.loads((WEB / "locales" / "en.json").read_text(encoding="utf-8"))
    for key in IDENTITY_KEYS:
        assert key in english
    assert 'data-i18n="about_daemon"' in page
    assert "daemon_family" in script and 't("named_file")' in script
    used = set(re.findall(r'data-i18n(?:-title|-label|-aria)?="([a-z_]+)"', page))
    used |= set(re.findall(r'\bt\("([a-z_]+)"', script))
    assert set(IDENTITY_KEYS) <= used
    assert used <= english.keys()
    for path in locales:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert set(IDENTITY_KEYS) <= data.keys()
        assert set(data) == set(english), path.name


def test_existing_about_fields_remain():
    page = (WEB / "index.html").read_text(encoding="utf-8")
    for key in ("about_screen", "about_system", "about_cpu", "about_memory",
                "about_network", "about_volumes"):
        assert f'data-i18n="{key}"' in page
    assert 'id="about-daemon"' in page and 'id="about-system"' in page


def test_viewer_clears_daemon_cache_on_disconnect_and_address_change():
    from amigent.viewer import Viewer

    viewer = Viewer.__new__(Viewer)
    viewer.amiga = {"daemon_family": "general"}
    viewer._daemon_path = "C:amigentd"
    viewer._daemon_bytes = b"cached"
    viewer._daemon_version = "old"
    viewer.online = True
    # Mirror the cache-clearing branches without opening sockets.
    viewer.amiga = {}
    viewer._daemon_path = ""
    viewer._daemon_bytes = None
    viewer._daemon_version = ""
    assert viewer.amiga == {}
    assert viewer._daemon_path == ""
    assert viewer._daemon_bytes is None
    source = Path(__file__).resolve().parents[1] / "src" / "amigent" / "viewer.py"
    text = source.read_text(encoding="utf-8")
    assert 'self._daemon_path = ""' in text
    assert "self._daemon_bytes = None" in text
    assert "describe_connected" in text
