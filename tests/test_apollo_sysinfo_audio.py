"""SYSINFO AUDIO capability advertisement must follow the Apollo hardware guard.

tests/c/sysinfo_apollo_harness.c compiles the verbatim CPU/Apollo/AUDIO and
EMU68 capability lines extracted from c_sysinfo() in amigentd.c together with
the verbatim apollo_os_here() and apollo_board_name() from apollo_detect.c.
Nothing touches 0xDFF3FC, arne.audio, or physical hardware.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from amigent.channel import AUDIO, EMU68, feature_opcode

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "src/native/amiga/daemon/amigentd.c"
DETECT = ROOT / "src/native/amiga/apollo/apollo_detect.c"
HARNESS = ROOT / "tests" / "c" / "sysinfo_apollo_harness.c"

AFF_68020 = 1 << 1
AFF_68030 = (1 << 2) | AFF_68020
AFF_68040 = (1 << 3) | AFF_68030
AFF_68060 = (1 << 7) | AFF_68040
AFF_68080 = 1 << 10
V4_SA_BOARD = 0x0500


def _c_sysinfo_body() -> str:
    source = DAEMON.read_text(encoding="utf-8")
    start = source.index("static BOOL c_sysinfo(struct link *l)")
    end = source.index("\n/* Blocks times block size", start)
    return source[start:end]


def _extract_sysinfo_cpu_block() -> str:
    body = _c_sysinfo_body()
    start = body.index("    {\n        UWORD f = SysBase->AttnFlags;")
    end = body.index("\n    /* From protocol 5:", start)
    return body[start:end] + "\n"


def _extract_sysinfo_emu68_line() -> str:
    body = _c_sysinfo_body()
    for line in body.splitlines():
        if 't_pair(&t, "emu68", emu68_present());' in line:
            return line + "\n"
    raise AssertionError("c_sysinfo() is missing the emu68 capability line")


def _extract_apollo_detect() -> str:
    source = DETECT.read_text(encoding="utf-8")
    return source[source.index("const char *apollo_board_name(UWORD board)"):]


def _compile_harness(folder: Path, name: str, *extra_flags: str) -> Path:
    exe = folder / name
    done = subprocess.run(
        [
            "gcc", "-O1", "-Wall", "-Wextra",
            *extra_flags,
            "-I", str(folder),
            "-I", str(ROOT / "tests" / "c" / "host"),
            "-o", str(exe),
            str(HARNESS),
        ],
        capture_output=True,
        text=True,
    )
    if done.returncode:
        pytest.fail(done.stderr[-3000:])
    assert done.stderr == ""
    return exe


@pytest.fixture(scope="module")
def sysinfo_harnesses(tmp_path_factory):
    if not shutil.which("gcc"):
        pytest.skip("no gcc")
    folder = tmp_path_factory.mktemp("sysinfo_apollo")
    (folder / "apollo_detect_extract.inc").write_text(_extract_apollo_detect(), encoding="utf-8")
    (folder / "sysinfo_apollo_extract.inc").write_text(_extract_sysinfo_cpu_block(), encoding="utf-8")
    (folder / "sysinfo_emu68_extract.inc").write_text(_extract_sysinfo_emu68_line(), encoding="utf-8")
    apollo_exe = _compile_harness(folder, "sysinfo_apollo", "-DAMIGENT_APOLLO")
    classic_exe = _compile_harness(folder, "sysinfo_classic")

    def run(exe: Path, exec_ver: int, attn: int, board: int = V4_SA_BOARD, emu68: int = 0) -> tuple[dict[str, str], int]:
        out = subprocess.run(
            [str(exe), str(exec_ver), hex(attn), hex(board), str(emu68)],
            capture_output=True,
            text=True,
            check=True,
        )
        info: dict[str, str] = {}
        for line in out.stdout.splitlines():
            key, val = line.split("=", 1)
            info[key] = val
        board_calls = int(info.pop("board_calls"))
        return info, board_calls

    return {
        "apollo": lambda *args, **kw: run(apollo_exe, *args, **kw),
        "classic": lambda *args, **kw: run(classic_exe, *args, **kw),
    }


def test_source_gates_audio_advertisement_on_existing_apollo_hardware_guard():
    block = _extract_sysinfo_cpu_block()
    apollo_branch, classic_branch = block.split("#else", 1)

    guard_header = "if (is_apollo || (f & (1L << 10))) {"
    assert guard_header in apollo_branch
    after_guard = apollo_branch.split(guard_header, 1)[1]
    guarded_body, after_block = after_guard.split("\n        }", 1)

    assert 't_pair(&t, "audio", CMD_AUDIO);' in guarded_body
    assert 't_pair(&t, "audio"' not in after_block
    assert 't_pair(&t, "audio"' not in classic_branch

    # SYSINFO must not probe arne.audio or add driver lookups.
    body = _c_sysinfo_body()
    for forbidden in ("arne.audio", "arne_mix", "OpenDevice", "FindName", "FindResident"):
        assert forbidden not in body


def test_apolloos_on_68080_advertises_audio_cpu_and_board(sysinfo_harnesses):
    info, board_calls = sysinfo_harnesses["apollo"](51, AFF_68080 | AFF_68040, V4_SA_BOARD, 0)
    assert info["cpu"] == "68080"
    assert info["os"] == "ApolloOS"
    assert info["board"] == "00000500"
    assert info["board_name"] == "V4-SA"
    assert info["audio"] == "49"
    assert info["emu68"] == "0"
    assert board_calls == 1
    assert feature_opcode(6, "AUDIO", info) == AUDIO == 49


def test_amigaos_on_68080_still_advertises_audio_without_apolloos(sysinfo_harnesses):
    info, board_calls = sysinfo_harnesses["apollo"](47, AFF_68080 | AFF_68040, V4_SA_BOARD, 0)
    assert info["cpu"] == "68080"
    assert info["os"] == "AmigaOS"
    assert info["board"] == "00000500"
    assert info["board_name"] == "V4-SA"
    assert info["audio"] == "49"
    assert info["emu68"] == "0"
    assert board_calls == 1
    assert feature_opcode(6, "AUDIO", info) == AUDIO == 49


@pytest.mark.parametrize(
    ("attn", "expected_cpu"),
    [
        (AFF_68020, "68020"),
        (AFF_68030, "68030"),
        (AFF_68040, "68040"),
        (AFF_68060, "68060"),
    ],
    ids=["68020", "68030", "68040", "68060"],
)
def test_classic_68020_to_68060_omits_audio_and_skips_board_register(sysinfo_harnesses, attn, expected_cpu):
    info, board_calls = sysinfo_harnesses["apollo"](47, attn, V4_SA_BOARD, 0)
    assert info["cpu"] == expected_cpu
    assert info["os"] == "AmigaOS"
    assert "audio" not in info
    assert "board" not in info
    assert "board_name" not in info
    assert board_calls == 0
    assert feature_opcode(6, "AUDIO", info) is None


def test_classic_68000_omits_audio_and_skips_board_register(sysinfo_harnesses):
    info, board_calls = sysinfo_harnesses["apollo"](40, 0, V4_SA_BOARD, 0)
    assert info["cpu"] == "68000"
    assert info["os"] == "AmigaOS"
    assert "audio" not in info
    assert "board" not in info
    assert "board_name" not in info
    assert board_calls == 0
    assert feature_opcode(6, "AUDIO", info) is None


def test_pistorm_emu68_does_not_advertise_contradictory_audio_49(sysinfo_harnesses):
    info, board_calls = sysinfo_harnesses["apollo"](47, AFF_68040, V4_SA_BOARD, 1)
    assert info["cpu"] == "68040"
    assert info["os"] == "AmigaOS"
    assert info["emu68"] == "1"
    assert "audio" not in info
    assert "board" not in info
    assert "board_name" not in info
    assert board_calls == 0
    assert feature_opcode(6, "EMU68", info) == EMU68 == 48
    assert feature_opcode(6, "AUDIO", info) is None


def test_idle_supported_apollo_advertises_audio_without_arne_driver_open(sysinfo_harnesses):
    # The host harness has no arne.audio device or Exec library list; SYSINFO
    # still advertises audio=49 purely from detected Apollo/68080 hardware.
    info, board_calls = sysinfo_harnesses["apollo"](51, AFF_68080, V4_SA_BOARD, 0)
    assert info["cpu"] == "68080"
    assert info["os"] == "ApolloOS"
    assert info["audio"] == "49"
    assert board_calls == 1
    assert feature_opcode(6, "AUDIO", info) == AUDIO == 49


@pytest.mark.parametrize(
    ("exec_ver", "attn", "expected_cpu", "emu68"),
    [
        (40, 0, "68000", 0),
        (47, AFF_68020, "68020", 0),
        (47, AFF_68040, "68040", 1),
        (47, AFF_68060, "68060", 0),
        (51, AFF_68080 | AFF_68040, "68040", 0),
    ],
    ids=["68000", "68020", "68040-emu68", "68060", "classic-ignores-68080-bit"],
)
def test_classic_non_apollo_build_retains_existing_sysinfo_behaviour(
    sysinfo_harnesses, exec_ver, attn, expected_cpu, emu68
):
    info, board_calls = sysinfo_harnesses["classic"](exec_ver, attn, V4_SA_BOARD, emu68)
    assert info["cpu"] == expected_cpu
    assert info["attn_flags"] == str(attn)
    assert info["emu68"] == str(emu68)
    assert "os" not in info
    assert "board" not in info
    assert "board_name" not in info
    assert "audio" not in info
    assert board_calls == 0
