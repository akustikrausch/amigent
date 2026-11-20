"""Screen pictures across a viewer handover, compiled from amiga/amigentd.c.

The harness is the daemon's own free_pictures() and lose_client(), plus the
takeover block in serve(). It runs on the host compiler. It does not reset
an Amiga or touch a ZZ9000.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "amiga" / "amigentd.c"


def _slice(code: str, start: str, end: str) -> str:
    at = code.index(start)
    return code[at:code.index(end, at)]


def _daemon() -> str:
    return SOURCE.read_text(encoding="latin-1")


def test_the_guard_frees_only_when_nobody_is_waiting():
    code = _daemon()
    lose = _slice(code, "static void lose_client(void)", "static void load_key(void)")
    pictures = _slice(code, "static void free_pictures(void)", "static void lose_client(void)")
    assert "for (i = 0; i < PENDING; i++)" in lose
    assert "pend[i].fd >= 0 && pend[i].screen" in lose
    assert lose.index("free_pictures()") > lose.index("return;")
    assert "zz.fails < ZZ_GIVE_UP" in pictures
    assert "zz_free();" in pictures
    assert "zz.reset = TRUE;" in pictures
    assert "zz.cur = NULL;" in pictures
    assert "#define ZZ_GIVE_UP 3" in code


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    gcc = shutil.which("gcc")
    if not gcc:
        pytest.skip("no gcc")
    code = _daemon()
    bodies = _slice(code, "static void free_pictures(void)", "static void load_key(void)")
    mark = code.index("/* the newcomer wins */")
    start = code.rindex("for (i = 0; i < PENDING && !(pend[i].fd >= 0 && pend[i].screen); i++)", 0, mark)
    cont = code.index("continue;\n", start)
    close = code.index("        }\n", cont)
    block = code[start:close + len("        }\n")]
    folder = tmp_path_factory.mktemp("handover")
    src = folder / "handover.c"
    src.write_text(
        (ROOT / "tests" / "c" / "handover_prelude.h").read_text(encoding="utf-8")
        + bodies
        + "static void handover(void)\n{\n    LONG i;\n    do {\n"
        + block
        + "    } while (0);\n}\n"
        + (ROOT / "tests" / "c" / "handover_main.c").read_text(encoding="utf-8"),
        encoding="utf-8")
    exe = folder / "handover"
    done = subprocess.run(
        [gcc, "-std=c99", "-Wall", "-Wextra", "-Werror", "-O1", "-o", str(exe), str(src)],
        capture_output=True, text=True)
    if done.returncode:
        pytest.fail(done.stderr[-3000:])
    return exe


def run(exe, name: str) -> str:
    done = subprocess.run([str(exe), name], capture_output=True, text=True)
    assert done.returncode == 0, (name, done.returncode, done.stderr)
    return done.stdout.strip()


def test_ordinary_disconnect_frees_the_pictures(harness):
    assert run(harness, "alone") == "drop=1 free=1 zz=1 reset=1 cur=0 frame=0 client=-1 mirror=0"


def test_a_waiting_replacement_keeps_the_pictures(harness):
    assert run(harness, "waiting") == "drop=1 free=0 zz=0 reset=0 cur=1 frame=1 client=-1 mirror=1"


def test_a_completed_replacement_keeps_the_pictures(harness):
    assert run(harness, "complete") == "drop=1 free=0 zz=0 reset=0 cur=1 frame=1 client=8 mirror=1"


def test_a_failed_replacement_frees_the_pictures(harness):
    assert run(harness, "fail") == "drop=2 free=1 zz=1 reset=1 cur=0 frame=0 client=-1 mirror=0"


def test_the_last_failed_replacement_frees_and_an_earlier_one_does_not(harness):
    assert run(harness, "two") == (
        "midfree=0 drop=4 free=1 zz=1 reset=1 cur=0 frame=0 client=-1 mirror=0")


def test_zz9000_buffers_stay_when_the_card_has_been_given_up(harness):
    assert run(harness, "zzgiveup") == "drop=1 free=1 zz=0 reset=1 cur=0 frame=0 client=-1 mirror=1"
