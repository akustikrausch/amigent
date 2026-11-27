"""Connected daemon identity in the viewer, separate from the computer."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from amigent import daemons
from amigent.daemons import SPECS, describe_connected, identify
from amigent.errors import AmigaError
from amigent.paths import AMIGA
from amigent.viewer import Viewer

BIN = AMIGA / "bin"
WEB = Path(__file__).resolve().parents[1] / "src" / "amigent" / "web"
IDENTITY_KEYS = (
    "about_daemon", "about_protocol", "about_daemon_cpu", "about_capabilities",
    "not_reported", "named_file",
)


def test_describe_connected_keeps_machine_and_file_identity_apart():
    classic = (BIN / "amigentd").read_bytes()
    on_fast = describe_connected(6, "amigentd 1.5.0", classic,
                                 {"os": "AmigaOS", "cpu": "68060", "loaded": "1"}, BIN)
    assert on_fast["daemon_family"] == "general"
    assert on_fast["daemon_target"] == "68020"
    assert on_fast["daemon_identity"] == "named file"
    assert on_fast["daemon_protocol"] == 6
    assert "loaded" in on_fast["daemon_capabilities"]
    # Host CPU/OS must not become the daemon family.
    assert on_fast["daemon_family"] != "68060"
    unnamed = describe_connected(5, "", None, {"os": "AmigaOS", "cpu": "68040"}, BIN)
    # "" is what the daemon did not tell: the page shows its own localised "not reported"
    assert unnamed["daemon_family"] == "" and unnamed["daemon_target"] == ""
    assert unnamed["daemon_identity"] == "" and unnamed["daemon_version"] == ""
    assert unnamed["daemon_protocol"] == 5


def test_classic_68000_packaged_daemon_identity():
    path = BIN / "amigentd.000"
    assert path.is_file()
    got = describe_connected(6, "amigentd 1.5.0", path.read_bytes(),
                             {"loaded": "1", "probe": "1"}, BIN)
    assert got["daemon_family"] == "68000"
    assert got["daemon_target"] == "68000"
    assert got["daemon_identity"] == "named file"
    assert set(got["daemon_capabilities"]) >= {"loaded", "probe"}


def test_unknown_bytes_are_named_file_without_invented_family():
    got = describe_connected(6, "custom 9.9.9", b"\x00\x00\x03\xf3not-a-packaged-daemon",
                             {"os": "ApolloOS", "cpu": "68080", "audio": "49"}, BIN)
    assert got["daemon_identity"] == "named file"
    assert got["daemon_family"] == "" and got["daemon_target"] == ""
    assert got["daemon_version"] == "custom 9.9.9"
    assert "AUDIO" in got["daemon_capabilities"]
    legacy = describe_connected(6, "custom 1.5.0", b"\x00\x00\x03\xf3not-a-packaged-daemon",
                                {"os": "ApolloOS", "cpu": "68080", "audio": "1"}, BIN)
    assert "AUDIO" in legacy["daemon_capabilities"]


def test_named_file_is_not_a_proven_running_binary():
    """A byte match says what the named file holds, never that this process runs it."""
    classic = (BIN / "amigentd").read_bytes()
    got = describe_connected(6, "amigentd 1.5.0", classic, {}, BIN)
    assert got["daemon_identity"] == "named file"
    assert got["daemon_identity"] not in ("running", "proven", "verified")
    assert "daemon_running" not in got and "daemon_proven" not in got


def test_apollo_packaged_daemon_identity_when_present():
    path = BIN / "amigentd.apollo"
    if not path.is_file():
        # The catalogue entry is tip's own; a packaged build is recognised when available.
        assert any(spec.name == "amigentd.apollo" for spec in SPECS)
        return
    got = describe_connected(6, "amigentd 1.5.0", path.read_bytes(),
                             {"audio": "1", "loaded": "1", "os": "ApolloOS", "cpu": "68080"}, BIN)
    assert got["daemon_family"] == "ApolloOS"
    assert got["daemon_target"] == "68080"
    assert got["daemon_identity"] == "named file"
    assert "AUDIO" in got["daemon_capabilities"]


def test_protocol_five_and_six_and_missing_capability_flags():
    classic = (BIN / "amigentd").read_bytes()
    p5 = describe_connected(5, "amigentd 1.3.0", classic, {}, BIN)
    assert p5["daemon_protocol"] == 5
    assert p5["daemon_capabilities"] == []
    p6 = describe_connected(6, "amigentd 1.5.0", classic,
                            {"loaded": "0", "poke": "0", "trace": "0", "audio": "0"}, BIN)
    assert p6["daemon_protocol"] == 6
    assert p6["daemon_capabilities"] == []
    poked = describe_connected(6, "amigentd 1.5.0", classic, {"poke": "1", "trace": "1"}, BIN)
    assert set(poked["daemon_capabilities"]) == {"POKE", "TRACE"}


def test_identify_matches_only_exact_packaged_bytes():
    classic = (BIN / "amigentd").read_bytes()
    assert identify(classic, BIN).name == "amigentd"
    assert identify(classic + b"x", BIN) is None
    assert identify(b"", BIN) is None


def test_malformed_version_and_zero_protocol_degrade():
    got = describe_connected(0, "   ", (BIN / "amigentd").read_bytes(), {}, BIN)
    assert got["daemon_protocol"] is None
    assert got["daemon_version"] == ""
    assert got["daemon_identity"] == "named file"


def test_the_catalogue_is_the_tips_and_describe_adds_no_second_one():
    source = Path(daemons.__file__).read_text(encoding="utf-8")
    assert source.count("SPECS: tuple[Spec, ...]") == 1
    assert source.count("def identify(") == 1 and source.count("def describe_connected(") == 1
    assert [s.name for s in SPECS] == ["amigentd.000", "amigentd", "amigentd.080", "amigentd.apollo"]
    # the selection side the tip owns is still there
    assert callable(daemons.choose) and callable(daemons.fits)


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
        assert set(IDENTITY_KEYS) <= data.keys(), path.name
        assert set(data) == set(english), path.name


def test_russian_identity_rows_are_translated():
    russian = json.loads((WEB / "locales" / "ru.json").read_text(encoding="utf-8"))
    english = json.loads((WEB / "locales" / "en.json").read_text(encoding="utf-8"))
    for key in IDENTITY_KEYS:
        assert russian[key] and russian[key] != english[key]
        assert re.search("[А-Яа-я]", russian[key])


def test_existing_about_fields_remain():
    page = (WEB / "index.html").read_text(encoding="utf-8")
    for key in ("about_screen", "about_system", "about_cpu", "about_memory",
                "about_network", "about_volumes"):
        assert f'data-i18n="{key}"' in page
    assert 'id="about-daemon"' in page and 'id="about-system"' in page


# --- the Viewer scopes the file to its own session ------------------------------------------

class FakeCtl:
    def __init__(self, blob: bytes, version: str = "amigentd 1.5.0"):
        self.blob, self.version = blob, version
        self.connected = True
        self.reads, self.runs = [], []

    def read_file(self, path):
        self.reads.append(path)
        return self.blob

    def exec(self, command):
        self.runs.append(command)
        return SimpleNamespace(output=f"{self.version}\n")

    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False


def _viewer(blob: bytes) -> Viewer:
    viewer = Viewer("192.0.2.10", screen_port=1, ctl_port=1)
    viewer.ctl = FakeCtl(blob)
    viewer.event = lambda *a, **k: None
    return viewer


def test_viewer_reads_the_named_file_once_and_reports_it():
    classic = (BIN / "amigentd").read_bytes()
    viewer = _viewer(classic)
    info = {"self": "C:amigentd", "loaded": "1"}
    first = viewer._connected_daemon(info, 6)
    again = viewer._connected_daemon(info, 6)
    assert first == again
    assert first["daemon_family"] == "general" and first["daemon_identity"] == "named file"
    assert first["daemon_version"] == "amigentd 1.5.0"
    assert viewer.ctl.reads == ["C:amigentd"]  # the second call came from the cache
    assert viewer.ctl.runs == ['Version "C:amigentd"']  # quoted: a space cannot split the command


def test_viewer_does_not_trust_a_cache_for_another_path():
    viewer = _viewer((BIN / "amigentd").read_bytes())
    viewer._connected_daemon({"self": "C:amigentd"}, 6)
    viewer._connected_daemon({"self": "SYS:C/amigentd"}, 6)
    assert viewer.ctl.reads == ["C:amigentd", "SYS:C/amigentd"]


@pytest.mark.parametrize("path", ["", 'C:evil"; Delete', "C:a\nb"])
def test_viewer_reads_no_file_without_a_clean_self_path(path):
    viewer = _viewer((BIN / "amigentd").read_bytes())
    got = viewer._connected_daemon({"self": path}, 6)
    assert viewer.ctl.reads == [] and got["daemon_identity"] == "" and got["daemon_family"] == ""
    assert got["daemon_protocol"] == 6


def test_viewer_degrades_when_the_file_cannot_be_read():
    viewer = _viewer(b"")

    def fail(path):
        raise AmigaError("object not found")

    viewer.ctl.read_file = fail
    got = viewer._connected_daemon({"self": "C:amigentd"}, 6)
    assert got["daemon_identity"] == "" and got["daemon_family"] == ""
    assert viewer._daemon_bytes is None


def _cached(viewer: Viewer) -> bool:
    return bool(viewer._daemon_path or viewer._daemon_bytes is not None or viewer._daemon_version)


def test_disconnect_clears_the_daemon_cache_and_the_facts():
    viewer = _viewer((BIN / "amigentd").read_bytes())
    viewer.online = True
    viewer.amiga = {"daemon_family": "general"}
    viewer._connected_daemon({"self": "C:amigentd"}, 6)
    assert _cached(viewer)
    viewer._set_online(False, "")
    assert not _cached(viewer) and viewer.amiga == {}


def test_retire_clears_the_daemon_cache():
    """An address edit or removal retires this Viewer; the new connection starts empty."""
    viewer = _viewer((BIN / "amigentd").read_bytes())
    viewer._connected_daemon({"self": "C:amigentd"}, 6)
    assert _cached(viewer)
    viewer.retire()
    assert not _cached(viewer)


def test_each_viewer_keeps_its_own_daemon_file():
    one, two = _viewer((BIN / "amigentd").read_bytes()), _viewer((BIN / "amigentd.000").read_bytes())
    a = one._connected_daemon({"self": "C:amigentd"}, 6)
    b = two._connected_daemon({"self": "C:amigentd"}, 6)
    assert a["daemon_family"] == "general" and b["daemon_family"] == "68000"
    one._set_online(False, "")
    assert not _cached(one) and _cached(two)


def test_two_viewers_with_different_identities_do_not_cross_contaminate():
    """Reading or refreshing one Viewer's About facts never touches the other's cache."""
    one, two = _viewer((BIN / "amigentd").read_bytes()), _viewer((BIN / "amigentd.000").read_bytes())
    info = {"self": "C:amigentd", "loaded": "1"}
    first = one._connected_daemon(info, 6)
    second = two._connected_daemon(info, 6)
    assert first["daemon_family"] == "general" and second["daemon_family"] == "68000"
    # both name the same path; each answer still comes from its own cached file
    assert one._connected_daemon(info, 6) == first
    assert two._connected_daemon(info, 6) == second
    assert one.ctl.reads == ["C:amigentd"] and two.ctl.reads == ["C:amigentd"]
    one.retire()
    assert not _cached(one) and _cached(two)
    assert two._connected_daemon(info, 6) == second
    assert two.ctl.reads == ["C:amigentd"]  # retiring the other Viewer cost it no re-read


def test_going_online_alone_keeps_the_cache_but_retire_drops_it():
    """Models a Viewer that rediscovers the same Amiga (DHCP keep) versus an address edit."""
    viewer = _viewer((BIN / "amigentd").read_bytes())
    viewer._connected_daemon({"self": "C:amigentd"}, 6)
    assert _cached(viewer) and not viewer.online
    viewer._set_online(True, "")  # reached again: the same Viewer, the same session
    assert viewer.online is True and _cached(viewer)
    viewer.retire()  # an address edit retires the Viewer: the next connection starts empty
    assert not _cached(viewer)


def test_the_named_file_is_found_by_size_and_crc_without_sending_it():
    import zlib
    classic = (BIN / "amigentd").read_bytes()
    viewer = _viewer(b"never sent")
    viewer.ctl.stat = lambda path: {"type": "file", "size": len(classic)}
    viewer.ctl.checksum = lambda path, size=0: zlib.crc32(classic)
    got = viewer._connected_daemon({"self": "C:amigentd"}, 6)
    assert got["daemon_family"] == "general" and viewer.ctl.reads == []
    other = _viewer(b"never sent")
    other.ctl.stat = lambda path: {"type": "file", "size": 1234}
    other.ctl.checksum = lambda path, size=0: 0
    got = other._connected_daemon({"self": "C:amigentd"}, 6)
    assert got["daemon_family"] == "" and got["daemon_identity"] == "named file" and other.ctl.reads == []
