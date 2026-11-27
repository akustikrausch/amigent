"""Stage 4 — Apollo TRACE set fail-closed (offline; no hardware)."""
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


def test_apollo_trace_set_refuses_before_trap_patch():
    trace = function_body(DAEMON, "c_trace")
    assert "AMIGENT_APOLLO" in trace
    assert "tracepoints are not supported on this Apollo build yet" in trace
    refuse = trace.split("unknown trace operation", 1)[1]
    assert refuse.index("AMIGENT_APOLLO") < refuse.index("0x4E4F")
    assert refuse.index("not supported on this Apollo") < refuse.index("0x4E4F")
    assert refuse.index("return c_fail") < refuse.index("*tr->at = 0x4E4F")


def test_list_and_clear_remain_before_apollo_set_refuse_path():
    trace = function_body(DAEMON, "c_trace")
    assert trace.index("op == 2") < trace.index("not supported on this Apollo")
    assert trace.index("op == 3") < trace.index("not supported on this Apollo")


def test_sysinfo_advertises_trace_zero_on_apollo():
    assert 't_pair(&t, "trace", 0)' in DAEMON
    assert "AMIGENT_APOLLO" in DAEMON.split('t_pair(&t, "trace", 0)', 1)[0][-80:]


def test_feature_opcode_hides_trace_when_sysinfo_says_zero():
    assert feature_opcode(6, "TRACE", {"loaded": "1", "trace": "0"}) is None
    assert feature_opcode(6, "TRACE", {"loaded": "1", "trace": "1"}) == 43
    assert feature_opcode(6, "TRACE", {"loaded": "1"}) == 43


def test_non_apollo_path_still_has_trap_arming():
    assert "*tr->at = 0x4E4F" in DAEMON
    assert "tc_TrapCode" in DAEMON
