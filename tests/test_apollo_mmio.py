"""The Apollo register guard, compiled on the host and checked against peek_allowed.

The harness calls apollo_mmio_hit. Its allow command simulates the order in
peek_allowed, including a MemHeader that claims the range. It does not read
or write a hardware address, and it is not a test on the V4.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MMIO = ROOT / "src/native/amiga/apollo" / "apollo_mmio.c"
DAEMON = ROOT / "src/native/amiga/daemon" / "amigentd.c"

# The windows named by apollo_mmio.c. Each pair is [start, end).
WINDOWS = (
    (0x00DFF1E6, 0x00DFF1E8),  # BPLHMOD
    (0x00DFF1EC, 0x00DFF1F0),  # BPLHPTH
    (0x00DFF1F4, 0x00DFF1F6),  # GFXMODE
    (0x00DFF254, 0x00DFF258),  # TEXDPT
    (0x00DFF3B0, 0x00DFF3C4),  # PIP 2
    (0x00DFF3D0, 0x00DFF3E4),  # PIP
    (0x00DFF3EA, 0x00DFF3FE),  # VREG, including VREG_BOARD
    (0x00DFF400, 0x00DFF500),  # ARNE
)


def windows_in_source() -> tuple[tuple[int, int], ...]:
    body = MMIO.read_text(encoding="utf-8").split("win[][2] = {", 1)[1].split("};", 1)[0]
    return tuple((int(a, 16), int(b, 16)) for a, b in re.findall(
        r"0x([0-9A-Fa-f]+)UL\s*,\s*0x([0-9A-Fa-f]+)UL", body))


def function_body(source: str, name: str) -> str:
    start = source.index(f"{name}(")
    return source[start:source.index("\nstatic ", start + 1)]


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    if not shutil.which("gcc"):
        pytest.skip("no gcc")
    folder = tmp_path_factory.mktemp("mmio")
    exe = folder / "mmio"
    done = subprocess.run(
        ["gcc", "-O1", "-Wall", "-Wextra", "-I", str(ROOT / "tests" / "c" / "host"),
         "-o", str(exe), str(MMIO), str(ROOT / "tests" / "c" / "mmio_harness.c")],
        capture_output=True, text=True)
    if done.returncode:
        pytest.fail(done.stderr[-3000:])
    assert done.stderr == ""
    return exe


def ask(exe, *args) -> int:
    done = subprocess.run([str(exe), *args], capture_output=True, text=True, check=True)
    return int(done.stdout.strip())


def test_the_source_table_is_the_existing_guard():
    assert windows_in_source() == WINDOWS


def test_peek_and_poke_consult_the_guard_before_touching_memory():
    source = DAEMON.read_text(encoding="utf-8")
    allowed = function_body(source, "peek_allowed")
    peek = function_body(source, "c_peek")
    poke = function_body(source, "c_poke")
    assert allowed.index("apollo_mmio_hit") < allowed.index("SysBase->MemList")
    assert allowed.index("#ifdef AMIGENT_APOLLO") < allowed.index("apollo_mmio_hit")
    assert peek.index("peek_allowed") < peek.index("t_add")
    # Hardened POKE: allow/region checks before the receive.
    assert poke.index("peek_allowed") < poke.index("l_recv") < poke.index("CopyMem")
    assert "CMD_PEEK" in source and "CMD_POKE" in source
    makefile = (ROOT / "src/native/amiga" / "Makefile.apollo").read_text(encoding="utf-8")
    assert "apollo_mmio.o" in makefile and "-DAMIGENT_APOLLO" in makefile


def test_ordinary_ram_is_still_allowed_when_a_header_covers_it(harness):
    assert ask(harness, "hit", "0x2000", "16") == 0
    assert ask(harness, "allow", "0x2000", "16", "1") == 1
    assert ask(harness, "allow", "0x2000", "16", "0") == 0
    assert ask(harness, "allow", "0xF80000", "2", "0") == 1
    assert ask(harness, "allow", "0x4", "4", "0") == 1


def test_every_window_and_its_edges_is_refused(harness):
    for start, stop in WINDOWS:
        size = stop - start
        assert ask(harness, "hit", hex(start), str(size)) == 1
        assert ask(harness, "allow", hex(start), str(size), "1") == 0
        assert ask(harness, "hit", hex(start - 2), "2") == 0
        assert ask(harness, "hit", hex(stop), "2") == 0
        assert ask(harness, "hit", hex(start - 2), "4") == 1
        assert ask(harness, "hit", hex(start + 1), str(size)) == 1
        assert ask(harness, "allow", hex(start - 2), "4", "1") == 0


def test_invalid_lengths_stay_refused_without_a_hardware_access(harness):
    assert ask(harness, "hit", "0x2000", "0") == 1
    assert ask(harness, "allow", "0x2000", "0", "1") == 0
    assert ask(harness, "hit", "0xFFFFFFFE", "4") == 1
    assert ask(harness, "allow", "0xFFFFFFFE", "4", "1") == 0
    assert ask(harness, "allow", "0x2000", "65537", "1") == 0
    assert ask(harness, "allow", "0x2000", "65536", "1") == 1


def test_a_covering_memheader_does_not_open_a_window(harness):
    assert ask(harness, "allow", "0xDFF000", "0x1000", "1") == 0
    assert ask(harness, "allow", "0xDFF1E6", "2", "1") == 0
    assert ask(harness, "allow", "0xDFF3FC", "2", "1") == 0
    assert ask(harness, "allow", "0xDFF400", "0x100", "1") == 0
