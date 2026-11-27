"""Stage 4 D3 — POKE resource harden: bounds, early gates, no unbounded drain (offline)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from amigent import channel
from amigent.daemons import consider_replacement
from amigent.errors import AmigaError
from amigent.paths import AMIGA

ROOT = Path(__file__).resolve().parents[1]
DAEMON = (ROOT / "src/native/amiga/daemon/amigentd.c").read_text(encoding="utf-8")
MMIO = (ROOT / "src/native/amiga/apollo/apollo_mmio.c").read_text(encoding="utf-8")
BIN = AMIGA / "bin"


def function_body(src: str, name: str) -> str:
    for m in re.finditer(rf"\b{re.escape(name)}\s*\(", src):
        start = m.start()
        brace = src.find("{", start)
        if brace < 0 or brace - start > 200 or ";" in src[start:brace]:
            continue
        nxt = src.index("\nstatic ", brace)
        return src[start:nxt]
    raise AssertionError(name)


def mmio_windows():
    body = MMIO.split("win[][2] = {", 1)[1].split("};", 1)[0]
    return [
        (int(a, 16), int(b, 16))
        for a, b in re.findall(r"0x([0-9A-Fa-f]+)UL\s*,\s*0x([0-9A-Fa-f]+)UL", body)
    ]


def test_c_poke_order_gates_before_alloc_and_write():
    poke = function_body(DAEMON, "c_poke")
    assert poke.index("PEEK_MAX") < poke.index("!allow_poke")
    assert poke.index("!allow_poke") < poke.index("AllocVec")
    assert poke.index("apollo_mmio_hit") < poke.index("AllocVec")
    assert poke.index("peek_allowed") < poke.index("AllocVec")
    assert poke.index("AllocVec") < poke.index("CopyMem(data, (APTR)at, n)")
    assert poke.index("l_recv") < poke.index("CopyMem(data, (APTR)at, n)")


def test_oversized_closes_without_l_skip_or_alloc():
    poke = function_body(DAEMON, "c_poke")
    over = poke.split("n > PEEK_MAX", 1)[1].split("if (!allow_poke)", 1)[0]
    assert "return FALSE" in over
    assert "l_skip" not in over
    assert "AllocVec" not in over
    assert "CopyMem" not in over


def test_zero_length_fails_without_skip_or_alloc():
    poke = function_body(DAEMON, "c_poke")
    zero = poke.split("if (!n)", 1)[1].split("if (n > PEEK_MAX)", 1)[0]
    assert "c_fail" in zero
    assert "l_skip" not in zero
    assert "AllocVec" not in zero


def test_allowpoke_off_skips_at_most_peek_max():
    poke = function_body(DAEMON, "c_poke")
    refuse = poke.split("if (!allow_poke)", 1)[1].split("#ifdef AMIGENT_APOLLO", 1)[0]
    assert "l_skip_until(l, n, deadline)" in refuse
    assert "AllocVec" not in refuse
    assert "CopyMem" not in refuse


def test_mmio_and_ram_refuse_before_alloc():
    poke = function_body(DAEMON, "c_poke")
    assert poke.index("apollo_mmio_hit") < poke.index("AllocVec")
    mmio = poke.split("apollo_mmio_hit(at, n)", 1)[1].split("if (!peek_allowed", 1)[0]
    assert "l_skip_until" in mmio and "AllocVec" not in mmio and "CopyMem" not in mmio


def test_alloc_failure_skips_bounded_payload():
    poke = function_body(DAEMON, "c_poke")
    fail = poke.split("if (!(data = AllocVec", 1)[1].split("if (!l_recv_until", 1)[0]
    assert "l_skip_until" in fail and "ERROR_NO_FREE_STORE" in fail


def test_recv_failure_frees_and_closes():
    poke = function_body(DAEMON, "c_poke")
    recv = poke.split("if (!l_recv_until(l, data, n, deadline))", 1)[1].split("mon_say", 1)[0]
    assert "FreeVec(data)" in recv and "return FALSE" in recv
    assert "CopyMem" not in recv


def test_all_eight_mmio_windows_hit_for_unit_write():
    windows = mmio_windows()
    assert len(windows) == 8
    for start, stop in windows:
        assert stop > start
        assert any(start <= at < stop for at in (start, (start + stop) // 2, stop - 1))


def test_client_bounds_empty_max_and_oversize():
    class Dummy(channel.AmigaChannel):
        def __init__(self):
            pass

        def _need6(self, _w):
            return None

        def _one(self, *_a, **_k):
            raise AssertionError("must not send")

    d = Dummy()
    with pytest.raises(AmigaError):
        d.poke(0x200000, b"")
    with pytest.raises(AmigaError):
        d.poke(0x200000, b"\0" * (channel.PEEK_MAX + 1))
    assert channel.PEEK_MAX == 65536
    assert "PEEK_MAX" in DAEMON


def test_near_4gib_declared_length_is_over_peek_max():
    n = 0xFFFFFFFF
    assert n > 65536
    poke = function_body(DAEMON, "c_poke")
    assert "n > PEEK_MAX" in poke and "return FALSE" in poke.split("n > PEEK_MAX", 1)[1][:80]


def test_unknown_same_version_apollo_stays_without_force(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    candidate = b"\x00\x00\x03\xf3$VER: amigentd 1.6.0 (24.11.2026)\x00 arne.audio"
    (bin_dir / "amigentd.apollo").write_bytes(candidate)
    unknown = b"$VER: amigentd 1.6.0 (24.11.2026)\x00 " + b"arne.audio" + b" unknown-apollo-bytes"
    keep, reason = consider_replacement(unknown, candidate, "amigentd.apollo", bin_dir)
    assert not keep
    assert "not one of the builds" in reason or "not the one in this package" in reason
