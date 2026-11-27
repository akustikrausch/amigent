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
    assert (native / "tools/amigent-levels.c").is_file()
    assert (native / "apollo/apollo_detect.c").is_file()
    assert (native / "apollo/apollo_probe.c").is_file()
    assert (native / "apollo/apollo_audio.c").is_file()
    assert (native / "apollo/apollo_audio.h").is_file()
    assert (native / "apollo/apollo_mmio.c").is_file()
    assert (native / "apollo/apollo_runtime.c").is_file()
    assert (native / "apollo/apollo_start.s").is_file()
    assert (native / "apollo/div64.c").is_file()
    assert (native / "apollo/gcc_support.c").is_file()
    assert (native / "Makefile.apollo").is_file()
    assert (native / "build-amigentd-apollo").is_file()
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
    for name in ("amigentd", "amigentd.000", "amigent-monitor", "amigent-status", "amigent-levels"):
        assert (bins / name).is_file()


def test_build_script_defaults_to_daemon_source():
    text = (ROOT / "src/native/amiga/build-amigentd").read_text(encoding="utf-8")
    assert ".DEF SOURCE daemon/amigentd.c" in text
    assert "src/native/amiga" in text


def test_apollo_makefile_roots_at_repository():
    text = (ROOT / "src/native/amiga/Makefile.apollo").read_text(encoding="utf-8")
    assert "ROOT := $(abspath $(HERE)/../../..)" in text
    assert "APOLLO := $(HERE)/apollo" in text


def test_guide_and_protocol_name_new_paths():
    guide = (ROOT / "docs/GUIDE.md").read_text(encoding="utf-8")
    protocol = (ROOT / "docs/PROTOCOL.md").read_text(encoding="utf-8")
    assert "src/native/amiga/build-amigentd" in guide
    assert "src/native/amiga/daemon/amigentd.c" in guide
    assert "src/native/amiga/daemon/trapstub.s" in protocol
    building = guide.split("## Building amigentd", 1)[1].split("## Translations", 1)[0]
    assert "src/native/amiga/daemon/amigentd.c" in building
    assert "amiga/amigentd.c" not in building
    assert "src/native/amiga/" in building


def test_amiga_workflow_uses_native_paths():
    text = (ROOT / ".github/workflows/amiga.yml").read_text(encoding="utf-8")
    assert 'paths: ["src/native/amiga/**"' in text
    assert "src/native/amiga/daemon/amigentd.c" in text
    assert "src/native/amiga/tools/amigent-status.c" in text
    assert "src/native/amiga/tools/amigent-monitor.c" in text
    assert "Makefile.apollo" in text
    assert "amigentd.apollo" in text
    assert "amigent-apollo-probe" in text
    assert "amigent-monitor.apollo" in text
    assert "amiga/amigentd.c" not in text
