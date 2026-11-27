"""The AUDIO stream's task lifecycle, offline: no Apollo, no sound.

tests/c/audio_harness.c runs amigentd's own stream code, from audio_send_some
to c_audio_stream, against a simulated Exec, timer.device, TLS pump, socket
and mix. Time only moves while the task sleeps (and 2 us per look at the
mix), so a spin shows up as looks in a row and a Wait() that nothing can end
is reported as a hang. Minutes of simulated time take milliseconds.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DAEMON = (ROOT / "src/native/amiga/daemon/amigentd.c").read_text(encoding="utf-8")
HARNESS = ROOT / "tests" / "c" / "audio_harness.c"
PLAYING = "play=0:1000000"


def extract() -> str:
    return DAEMON[DAEMON.index("/* What send() accepted"):DAEMON.index("/* AUDIO is its own answer")]


@pytest.fixture(scope="module")
def stream(tmp_path_factory):
    if not shutil.which("gcc"):
        pytest.skip("no gcc")
    folder = tmp_path_factory.mktemp("audio")
    (folder / "audio_extract.inc").write_text(extract(), encoding="utf-8")
    exe = folder / "audio"
    done = subprocess.run(
        ["gcc", "-O1", "-Wall", "-Wextra", "-I", str(folder), "-I", str(ROOT / "tests" / "c" / "host"),
         "-o", str(exe), str(HARNESS)], capture_output=True, text=True)
    if done.returncode:
        pytest.fail(done.stderr[-3000:])
    assert done.stderr == ""

    def run(*args: str) -> list[dict[str, int]]:
        out = subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, f"{out.stdout} (exit {out.returncode})"
        cycles = [dict((k, int(v)) for k, v in re.findall(r"(\w+)=(-?\d+)", line))
                  for line in out.stdout.splitlines() if line.startswith("cycle=")]
        assert cycles and "fault=" not in out.stdout, out.stdout
        return cycles
    return run


def tidy(r: dict[str, int]) -> None:
    """Nothing left behind: claim, port, timer request, flags, priority, signal."""
    assert r["free"] == 1, "the claim is still taken"
    assert r["ports"] == r["ios"] == r["io_out"] == 0
    assert r["hold"] == r["wait"] == r["task"] == 0
    assert r["pri"] == 0 and r["portsig"] == 0 and r["faults"] == 0


@pytest.mark.parametrize("tls", ["1", "0"], ids=["tls", "plain"])
def test_a_stream_starts_and_stops(stream, tls):
    (r,) = stream(f"tls={tls}", PLAYING, "stop=2000")
    assert r["ret"] == 1 and 2000 <= r["end_ms"] <= 2025
    assert r["made"] > 0 and r["sent"] >= r["made"]  # every half went out
    tidy(r)


@pytest.mark.parametrize("close", ["0", "30000"], ids=["closed", "half-open for 30 s"])
def test_a_pc_that_goes_away_gives_the_claim_back(stream, close):
    (r,) = stream("tls=1", PLAYING, "gone=1500", f"close={close}")
    assert r["ret"] == 0 and r["end_ms"] <= 1500 + int(close) + 100
    tidy(r)


def test_a_pump_that_is_quicker_than_the_task_does_not_strand_it(stream):
    (r,) = stream("tls=1", PLAYING, "stop=2000", "pump=0")
    assert r["ret"] == 1 and r["end_ms"] <= 2025 and r["sent"] >= r["made"]
    tidy(r)


def test_a_slow_pump_only_slows_the_stream(stream):
    (r,) = stream("tls=1", PLAYING, "stop=2000", "pump=50000")  # longer than AUDIO_PUMP_US
    assert r["ret"] == 1 and r["sent"] >= r["made"]
    tidy(r)


def test_long_silence_neither_blocks_nor_spins(stream):
    (r,) = stream("tls=1", "play=0:500", "stop=120000", "limit=200000")
    assert r["ret"] == 1 and 120000 <= r["end_ms"] <= 120050
    assert r["busy5"] <= 3 and r["looks5"] < 5000
    tidy(r)


@pytest.mark.parametrize("tls", ["1", "0"], ids=["tls", "plain"])
def test_break_is_served_at_once(stream, tls):
    (r,) = stream(f"tls={tls}", PLAYING, "brk=1000")
    assert r["ret"] == 0 and r["end_ms"] <= 1025
    tidy(r)


@pytest.mark.parametrize("tls", ["1", "0"], ids=["tls", "plain"])
def test_while_nothing_new_plays_the_task_sleeps_at_priority_5(stream, tls):
    (r,) = stream(f"tls={tls}", PLAYING, "stop=5000")
    assert r["busy5"] <= 3, "looks at priority 5 without a sleep in between"
    assert r["looks5"] <= 5 * 1100  # about one look a millisecond, asleep in between
    assert r["sleeps"] >= r["looks5"] // 2
    tidy(r)


def test_without_a_timer_request_the_vblank_stands_in(stream):
    (r,) = stream("tls=1", PLAYING, "gone=1500", "close=30000", "notimer=1", "limit=60000")
    assert r["ret"] == 0 and r["end_ms"] <= 31600 and r["busy5"] <= 3
    tidy(r)


def test_streams_one_after_another_leave_nothing_behind(stream):
    cycles = stream("tls=1", PLAYING, "stop=1500", "cycles=5")
    assert len(cycles) == 5
    for r in cycles:
        assert r["ret"] == 1
        tidy(r)
    first = {k: v for k, v in cycles[0].items() if k != "cycle"}
    assert all({k: v for k, v in r.items() if k != "cycle"} == first for r in cycles)


def test_the_tls_wait_has_a_timer_and_the_claim_one_way_out():
    follow = DAEMON[DAEMON.index("static BOOL audio_follow("):DAEMON.index("/* What is already queued")]
    assert "Wait(" not in follow  # every sleep goes through yield_wait or yield_us
    assert "yield_wait(nap_port, nap, AUDIO_PUMP_US, SIGBREAKF_CTRL_C | SIGBREAKF_CTRL_D)" in follow
    assert follow.index("audio_wait = 1") < follow.index("audio_push(l, out, out_len, out_sent, *rate, 16384)")
    stream_body = DAEMON[DAEMON.index("static BOOL c_audio_stream("):DAEMON.index("/* AUDIO is its own answer")]
    assert stream_body.count("apollo_audio_release()") == 2  # the failed hello, and the one end
    assert "return" not in stream_body.split("while (ok) {", 1)[1].split("apollo_audio_release();", 1)[0]
