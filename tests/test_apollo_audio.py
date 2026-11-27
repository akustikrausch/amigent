"""Protocol 6 AUDIO uses unique opcode 49 (CMD_AUDIO); EMU68 remains on 48.

These tests check opcode uniqueness, mixed-version compatibility (audio=49 vs
legacy fork audio=1), capability flags and wire bytes. Nothing contacts hardware.
"""

import re
import struct
from pathlib import Path

import pytest

from amigent.channel import (
    AUDIO, CHECKSUM, CMD_AUTH, CMD_CHANNEL, COMPRESS, CPU, DEBUGLOG, DELETE, DIR,
    DRAG, EMU68, EXEC, LOADED, LOCKS, MAKEDIR, NOTE, PEEK, POKE, QUIET, READ,
    RENAME, STAT, SYSINFO, TASKCTL, TASKS, TEXT, TRACE, TREE, VOLUMES, WAIT, WRITE,
    AmigaChannel, AmigaError, audio_request, feature_opcode,
)

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_37_47 = (NOTE, LOADED, PEEK, TASKS, TASKCTL, POKE, TRACE, LOCKS, CPU, DRAG, QUIET)


def _c_command_opcodes() -> list[tuple[str, int]]:
    daemon = (ROOT / "src/native/amiga/daemon/amigentd.c").read_text(encoding="utf-8")
    enum_block = daemon.split("enum {", 1)[1].split("};", 1)[0]
    cleaned = re.sub(r"/\*.*?\*/", "", enum_block, flags=re.S)
    entries = []
    nxt = 0
    for line in cleaned.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for part in line.split(","):
            token = part.strip()
            if not token:
                continue
            if "=" in token:
                name, val = [x.strip() for x in token.split("=", 1)]
                nxt = int(val, 0)
            else:
                name = token
            entries.append((name, nxt))
            nxt += 1
    return entries


def test_every_command_has_a_unique_opcode_in_python_and_c():
    python_ops = {
        "CMD_AUTH": CMD_AUTH, "CMD_CHANNEL": CMD_CHANNEL,
        "EXEC": EXEC, "READ": READ, "WRITE": WRITE, "DIR": DIR, "STAT": STAT,
        "DELETE": DELETE, "RENAME": RENAME, "MAKEDIR": MAKEDIR, "CHECKSUM": CHECKSUM,
        "SYSINFO": SYSINFO, "VOLUMES": VOLUMES, "TEXT": TEXT, "WAIT": WAIT,
        "COMPRESS": COMPRESS, "TREE": TREE, "DEBUGLOG": DEBUGLOG, "NOTE": NOTE,
        "LOADED": LOADED, "PEEK": PEEK, "TASKS": TASKS, "TASKCTL": TASKCTL,
        "POKE": POKE, "TRACE": TRACE, "LOCKS": LOCKS, "CPU": CPU, "DRAG": DRAG,
        "QUIET": QUIET, "EMU68": EMU68, "AUDIO": AUDIO,
    }
    assert len(set(python_ops.values())) == len(python_ops)
    assert UPSTREAM_37_47 == tuple(range(37, 48))
    assert EMU68 == 48
    assert AUDIO == 49
    assert EMU68 != AUDIO

    c_ops = _c_command_opcodes()
    c_map = dict(c_ops)
    assert len(set(c_map.values())) == len(c_ops)
    assert c_map["CMD_EMU68"] == 48
    assert c_map["CMD_AUDIO"] == 49
    assert c_map["CMD_EMU68"] != c_map["CMD_AUDIO"]


def test_protocol_6_audio_is_49_only_when_advertised_49():
    assert feature_opcode(6, "AUDIO", {"audio": "49"}) == 49
    assert feature_opcode(6, "AUDIO", {}) is None
    assert feature_opcode(6, "AUDIO", {"audio": "0"}) is None
    assert feature_opcode(6, "AUDIO", {"audio": "1"}) is None  # legacy fork opcode-48 refused
    assert feature_opcode(6, "LOADED", {"loaded": "1", "audio": "49"}) == LOADED == 38
    assert feature_opcode(5, "AUDIO", {"audio": "49"}) is None
    assert feature_opcode(4, "AUDIO", {"audio": "49"}) is None


def test_emu68_stays_48_and_refuses_legacy_audio_1_overload():
    assert feature_opcode(6, "EMU68", {"emu68": "1"}) == 48
    assert feature_opcode(6, "EMU68", {"emu68": "0", "audio": "49"}) == 48
    assert feature_opcode(6, "EMU68", {"emu68": "0", "audio": "1"}) is None

    channel = AmigaChannel.__new__(AmigaChannel)
    channel.protocol = 6
    channel._sock = object()
    channel.sysinfo = lambda: {"emu68": "0", "audio": "1"}
    with pytest.raises(AmigaError, match="legacy audio=1 on opcode 48"):
        channel._need_emu68("asking Emu68")


def test_audio_headers_are_ten_bytes_on_opcode_49():
    assert audio_request() == struct.pack(">BBHHHH", 49, 0, 0, 0, 0, 0)
    assert audio_request(1, 200) == struct.pack(">BBHHHH", 49, 1, 0, 200, 0, 0)
    assert audio_request(2, 0) == struct.pack(">BBHHHH", 49, 2, 0, 0, 0, 0)
    with pytest.raises(AmigaError):
        audio_request(protocol=5)
    with pytest.raises(AmigaError):
        audio_request(protocol=4)


def test_one_shot_and_stream_use_opcode_49_and_reject_legacy_audio_1():
    channel = AmigaChannel.__new__(AmigaChannel)
    channel.protocol = 6
    channel._sock = type("Sock", (), {"settimeout": lambda self, t: None})()
    channel.timeout = 30
    channel._audio_opcode = 0
    channel.sysinfo = lambda: {"audio": "49"}
    sent = []
    channel._request = lambda cmd, **kw: sent.append((cmd, kw))
    blobs = iter([struct.pack(">II", 48000, 4), b"\x01\x00\x02\x00"])
    channel._exact = lambda n: next(blobs)

    rate, pcm = channel.audio(200)
    assert sent == [(49, {"y": 200})]
    assert rate == 48000 and pcm == b"\x01\x00\x02\x00"

    channel.audio_start()
    assert sent[-1] == (49, {"arg": 1})
    channel._exact = lambda n: struct.pack(">II", 48000, 0xFFFFFFFF)
    assert channel.audio_read() is None
    headers = []
    channel._header = lambda cmd, arg=0, **kw: headers.append((cmd, arg))
    channel.audio_stop()
    assert headers == [(49, 2)]

    legacy = AmigaChannel.__new__(AmigaChannel)
    legacy.protocol = 6
    legacy._sock = object()
    legacy.sysinfo = lambda: {"audio": "1"}
    with pytest.raises(AmigaError, match="legacy audio=1.*opcode 49"):
        legacy.audio()
    with pytest.raises(AmigaError, match="legacy audio=1.*opcode 49"):
        legacy.audio_start()


def test_a_classic_daemon_is_not_offered_audio():
    channel = AmigaChannel.__new__(AmigaChannel)
    channel.protocol = 6
    channel._sock = object()
    channel.sysinfo = lambda: {"cpu": "68020", "loaded": "1", "emu68": "0"}
    with pytest.raises(AmigaError, match="Apollo"):
        channel.audio()


def test_the_daemon_implements_audio_49_and_keeps_emu68_48():
    daemon = (ROOT / "src/native/amiga/daemon/amigentd.c").read_text(encoding="utf-8")
    audio = (ROOT / "src/native/amiga/apollo/apollo_audio.c").read_text(encoding="utf-8")
    assert "case CMD_AUDIO:" in daemon
    assert "case CMD_EMU68:" in daemon
    assert "apollo_audio(" in audio and "arne.audio" in audio
    assert "apollo_pip" not in daemon and "apollo_pip" not in audio
    assert "c_pip" not in daemon
    assert "CMD_PIP" not in daemon and "CMD_PIP" not in audio
    assert 't_pair(&t, "audio", CMD_AUDIO)' in daemon
    assert 't_pair(&t, "pip"' not in daemon
    makefile = (ROOT / "src/native/amiga/Makefile.apollo").read_text(encoding="utf-8")
    assert "apollo_audio.o" in makefile
    assert "apollo.o" not in makefile and "ammx" not in makefile
