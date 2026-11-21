"""Native Amiga sources live under src/native and tests/native."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_native_amiga_tree_exists():
    native = ROOT / "src/native/amiga"
    assert (native / "daemon/amigentd.c").is_file()
    assert (native / "daemon/hookstub.s").is_file()
    assert (native / "daemon/trapstub.s").is_file()
    assert (native / "tools/amigent-monitor.c").is_file()
    assert (native / "tools/amigent-status.c").is_file()
    assert (native / "examples/repair.d/example.repair.txt").is_file()
    assert (native / "build-amigentd").is_file()
    assert not (ROOT / "amiga").exists()


def test_native_amiga_tests_exist():
    tests = ROOT / "tests/native/amiga"
    assert (tests / "pointer-test.c").is_file()
    assert (tests / "kprintf-test.c").is_file()
    assert (tests / "trace-test.c").is_file()


def test_packaged_binaries_stay_under_amigent_package():
    bins = ROOT / "src/amigent/amiga/bin"
    for name in ("amigentd", "amigentd.000", "amigent-monitor", "amigent-status"):
        assert (bins / name).is_file()


def test_build_script_defaults_to_daemon_source():
    text = (ROOT / "src/native/amiga/build-amigentd").read_text(encoding="utf-8")
    assert ".DEF SOURCE daemon/amigentd.c" in text
    assert "src/native/amiga" in text


def test_guide_and_protocol_name_new_paths():
    guide = (ROOT / "docs/GUIDE.md").read_text(encoding="utf-8")
    protocol = (ROOT / "docs/PROTOCOL.md").read_text(encoding="utf-8")
    assert "src/native/amiga/build-amigentd" in guide
    assert "src/native/amiga/daemon/amigentd.c" in guide
    assert "src/native/amiga/daemon/trapstub.s" in protocol
    # Primary build instructions use the new path; the PR #26 note may quote the old path.
    building = guide.split("## Building amigentd", 1)[1].split("## Translations", 1)[0]
    primary, _, note = building.partition("When the separate")
    assert "src/native/amiga/daemon/amigentd.c" in primary
    assert "amiga/amigentd.c" not in primary
    assert "PR #26" in note
