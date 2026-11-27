"""Combined D3 POKE + Apollo TRACE harden coexist (offline)."""
from __future__ import annotations

import re
from pathlib import Path

from amigent.channel import feature_opcode

ROOT = Path(__file__).resolve().parents[1]
DAEMON = (ROOT / "src/native/amiga/daemon/amigentd.c").read_text(encoding="utf-8")


def function_body(src: str, name: str) -> str:
    for m in re.finditer(rf"\b{re.escape(name)}\s*\(", src):
        start = m.start()
        brace = src.find("{", start)
        if brace < 0 or brace - start > 200 or ";" in src[start:brace]:
            continue
        return src[start : src.index("\nstatic ", brace)]
    raise AssertionError(name)


def test_combined_source_has_both_corrections():
    poke = function_body(DAEMON, "c_poke")
    assert "n > PEEK_MAX" in poke and "return FALSE" in poke.split("n > PEEK_MAX", 1)[1][:80]
    assert poke.index("!allow_poke") < poke.index("AllocVec")
    assert poke.index("apollo_mmio_hit") < poke.index("CopyMem")
    assert "CopyMem(data, (APTR)at, n)" in poke
    trace = function_body(DAEMON, "c_trace")
    assert "not supported on this Apollo build yet" in trace
    assert trace.index("not supported on this Apollo") < trace.index("*tr->at = 0x4E4F")
    assert 't_pair(&t, "trace", 0)' in DAEMON


def test_client_hides_trace_when_advertised_off():
    assert feature_opcode(6, "TRACE", {"loaded": "1", "trace": "0"}) is None
    assert feature_opcode(6, "TRACE", {"loaded": "1"}) == 43
