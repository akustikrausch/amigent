"""POKE payload timing: every amigentd build, offline, no hardware.

The receive helpers and poke_budget_ms are copied out of amigentd.c and run
by tests/c/poke_harness.c against a simulated sender and clock, so minutes of
a slow link take milliseconds here.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DAEMON = (ROOT / "src/native/amiga/daemon/amigentd.c").read_text(encoding="utf-8")
HARNESS = ROOT / "tests" / "c" / "poke_harness.c"
IO_MS = 30_000
KB64 = 65536


def function_body(src: str, name: str) -> str:
    for m in re.finditer(rf"\b{re.escape(name)}\s*\(", src):
        start = m.start()
        brace = src.find("{", start)
        if brace < 0 or brace - start > 200 or ";" in src[start:brace]:
            continue
        return src[start : src.index("\nstatic ", brace)]
    raise AssertionError(name)


def extract() -> str:
    """The daemon's own code under test, with the two limits it reads."""
    helpers = DAEMON[DAEMON.index("/* Waits as l_wait does"):DAEMON.index("/* ---- newcomers")]
    start = DAEMON.index("#define POKE_RATE")
    budget = DAEMON[start:DAEMON.index("\n}\n", start) + 3]
    limits = "".join(re.search(rf"^#define {name} .*\n", DAEMON, re.M).group(0)
                     for name in ("IO_SECONDS", "PEEK_MAX"))
    return limits + helpers + budget


@pytest.fixture(scope="module")
def poke(tmp_path_factory):
    if not shutil.which("gcc"):
        pytest.skip("no gcc")
    folder = tmp_path_factory.mktemp("poke")
    (folder / "poke_extract.inc").write_text(extract(), encoding="utf-8")
    exe = folder / "poke"
    done = subprocess.run(
        ["gcc", "-O1", "-Wall", "-Wextra", "-I", str(folder), "-I", str(ROOT / "tests" / "c" / "host"),
         "-o", str(exe), str(HARNESS)], capture_output=True, text=True)
    if done.returncode:
        pytest.fail(done.stderr[-3000:])
    assert done.stderr == ""

    def run(*args) -> tuple[str, int, int] | int:
        out = subprocess.run([str(exe), *map(str, args)], capture_output=True, text=True, check=True)
        words = out.stdout.split()
        if args[0] == "budget":
            return int(words[0])
        return words[0], int(words[1]), int(words[2])
    return run


def test_a_small_payload_arrives_at_once(poke):
    assert poke("recv", 16, 0, 16) == ("ok", 0, 16)


def test_a_64kb_payload_at_a_slow_link_takes_longer_than_30_s_and_arrives(poke):
    result, took, got = poke("recv", KB64, 1000, KB64)  # about 1 KB a second
    assert result == "ok" and got == KB64
    assert took > IO_MS  # the old single 30 s deadline would have closed it here


def test_the_slowest_rate_allowed_still_gets_64kb_through(poke):
    result, took, got = poke("recv", KB64, 256, KB64)
    assert result == "ok" and got == KB64 and took <= poke("budget", KB64)


def test_a_sender_that_stops_is_dropped_after_the_idle_time(poke):
    result, took, got = poke("recv", KB64, 0, 4096)
    assert (result, took, got) == ("timeout", IO_MS, 4096)
    result, took, got = poke("recv", KB64, 2048, 8192)  # sends for 4 s, then nothing
    assert result == "timeout" and got == 8192 and took == 4000 + IO_MS


def test_a_trickle_ends_at_the_budget(poke):
    budget = poke("budget", KB64)
    result, took, got = poke("recv", KB64, 50, KB64)  # a byte every 20 ms, never idle
    assert result == "timeout" and took == budget and got < KB64


def test_a_refused_poke_drains_on_the_same_terms(poke):
    assert poke("skip", KB64, 1000, KB64)[0] == "ok"
    assert poke("skip", KB64, 0, 100)[:2] == ("timeout", IO_MS)
    assert poke("skip", KB64, 50, KB64)[:2] == ("timeout", poke("budget", KB64))


def test_the_budget_grows_with_n_and_is_bounded(poke):
    assert poke("budget", 0) == IO_MS
    assert poke("budget", 256) == IO_MS + 1000
    assert poke("budget", KB64) == IO_MS + 256_000
    for n in (KB64 + 1, 0x7FFFFFFF, 0xFFFFFFFF):  # no overflow, whatever n says
        assert poke("budget", n) == poke("budget", KB64)


def test_the_deadline_survives_the_millisecond_clock_wrapping(poke):
    assert poke("recv", KB64, 1000, KB64, 0xFFFFFFFF - 10_000)[:2] == ("ok", 65536)
    assert poke("recv", 16, 0, 0, 0xFFFFFFFF - 10_000)[:2] == ("timeout", IO_MS)


def test_c_poke_takes_the_budget_once_before_any_payload_io():
    body = function_body(DAEMON, "c_poke")
    assert "deadline = now_ms() + poke_budget_ms(n);" in body
    assert body.index("poke_budget_ms") < body.index("l_skip_until")
    assert body.index("poke_budget_ms") < body.index("l_recv_until")
    assert "IO_SECONDS)" not in body  # no idle-only receive left in the payload phase


def test_oversized_still_closes_without_skip_or_alloc():
    body = function_body(DAEMON, "c_poke")
    over = body.split("n > PEEK_MAX", 1)[1].split("deadline =", 1)[0]
    assert "return FALSE" in over
    assert "l_skip" not in over and "AllocVec" not in over


def test_allowpoke_mmio_ram_use_skip_until_then_fail():
    body = function_body(DAEMON, "c_poke")
    for marker in ("!allow_poke", "apollo_mmio_hit", "!peek_allowed"):
        assert "l_skip_until" in body.split(marker, 1)[1][:200]


def test_incomplete_recv_until_frees_and_closes():
    body = function_body(DAEMON, "c_poke")
    fail = body.split("l_recv_until", 1)[1].split("mon_say", 1)[0]
    assert "FreeVec(data)" in fail and "return FALSE" in fail
    assert "CopyMem" not in fail


def test_rejected_paths_never_reach_copymem_before_gates():
    body = function_body(DAEMON, "c_poke")
    assert body.rindex("l_recv_until") < body.index("CopyMem(data, (APTR)at, n)")
    assert body.index("!allow_poke") < body.index("CopyMem(data, (APTR)at, n)")


def test_payload_bounds_are_unchanged():
    assert re.search(r"#define PEEK_MAX 65536", DAEMON)
    assert "#define IO_SECONDS 30" in DAEMON
    body = function_body(DAEMON, "c_poke")
    assert "n > PEEK_MAX" in body and "if (!n)" in body
