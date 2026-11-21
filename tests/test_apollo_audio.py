"""Protocol 6 AUDIO is opcode 48. PIP is not a command in this tree.

These tests check numbering, capability flags and the bytes on the wire.
They do not play audio and they do not run on Apollo hardware.
"""

import struct

import pytest

from amigent.channel import (
    AUDIO, CPU, DRAG, LOADED, LOCKS, NOTE, PEEK, POKE, QUIET, TASKCTL, TASKS, TRACE,
    AmigaChannel, AmigaError, audio_request, feature_opcode,
)
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = (NOTE, LOADED, PEEK, TASKS, TASKCTL, POKE, TRACE, LOCKS, CPU, DRAG, QUIET)


def test_upstream_opcodes_37_through_47_stay_put():
    assert UPSTREAM == tuple(range(37, 48))
    assert AUDIO == 48


def test_protocol_6_audio_is_48_only_when_advertised():
    assert feature_opcode(6, "AUDIO", {"audio": "1"}) == 48
    assert feature_opcode(6, "AUDIO", {}) is None
    assert feature_opcode(6, "AUDIO", {"audio": "0"}) is None
    assert feature_opcode(6, "LOADED", {"loaded": "1", "audio": "1"}) == LOADED == 38
    assert feature_opcode(6, "AUDIO", {"loaded": "1", "audio": "1"}) != 38


def test_protocol_5_keeps_the_published_audio_opcode():
    advertised = {"audio": "1", "pip": "1", "modules": "1", "tasklist": "1"}
    assert feature_opcode(5, "AUDIO", advertised) == 38
    assert feature_opcode(5, "PIP", advertised) == 39
    assert feature_opcode(5, "LOADED", advertised) == 40
    assert feature_opcode(5, "TASKS", advertised) == 41
    assert feature_opcode(5, "AUDIO", {}) is None
    assert feature_opcode(5, "PIP", {"pip": "0"}) is None
    assert feature_opcode(4, "AUDIO", advertised) is None


def test_pip_stays_unsent_unless_a_daemon_advertises_it():
    assert feature_opcode(6, "PIP", {}) is None
    assert feature_opcode(6, "PIP", {"pip": "0", "audio": "1"}) is None
    assert feature_opcode(6, "PIP", {"pip": "1"}) == 49
    assert not hasattr(AmigaChannel, "pip")


def test_audio_headers_are_ten_bytes():
    assert audio_request() == struct.pack(">BBHHHH", 48, 0, 0, 0, 0, 0)
    assert audio_request(1, 200) == struct.pack(">BBHHHH", 48, 1, 0, 200, 0, 0)
    assert audio_request(2, 0, protocol=5) == struct.pack(">BBHHHH", 38, 2, 0, 0, 0, 0)
    with pytest.raises(AmigaError):
        audio_request(protocol=4)


def test_one_shot_and_stream_use_opcode_48_and_the_documented_framing():
    channel = AmigaChannel.__new__(AmigaChannel)
    channel.protocol = 6
    channel._sock = type("Sock", (), {"settimeout": lambda self, t: None})()
    channel.timeout = 30
    channel._audio_opcode = 0
    channel.sysinfo = lambda: {"audio": "1", "pip": "0"}
    sent = []
    channel._request = lambda cmd, **kw: sent.append((cmd, kw))
    blobs = iter([struct.pack(">II", 48000, 4), b"\x01\x00\x02\x00"])
    channel._exact = lambda n: next(blobs)

    rate, pcm = channel.audio(200)
    assert sent == [(48, {"y": 200})]
    assert rate == 48000 and pcm == b"\x01\x00\x02\x00"

    channel.audio_start()
    assert sent[-1] == (48, {"arg": 1})
    channel._exact = lambda n: struct.pack(">II", 48000, 0xFFFFFFFF)
    assert channel.audio_read() is None
    headers = []
    channel._header = lambda cmd, arg=0, **kw: headers.append((cmd, arg))
    channel.audio_stop()
    assert headers == [(48, 2)]


def test_a_classic_daemon_is_not_offered_audio():
    channel = AmigaChannel.__new__(AmigaChannel)
    channel.protocol = 6
    channel._sock = object()
    channel.sysinfo = lambda: {"cpu": "68020", "loaded": "1"}
    with pytest.raises(AmigaError, match="Apollo"):
        channel.audio()


def test_the_daemon_implements_audio_48_and_not_pip():
    daemon = (ROOT / "amiga" / "amigentd.c").read_text(encoding="utf-8")
    audio = (ROOT / "amiga" / "apollo" / "apollo_audio.c").read_text(encoding="utf-8")
    assert "case CMD_AUDIO:" in daemon
    assert "apollo_audio(" in audio and "arne.audio" in audio
    assert "apollo_pip" not in daemon and "apollo_pip" not in audio
    assert "c_pip" not in daemon
    assert "CMD_PIP" not in daemon and "CMD_PIP" not in audio
    assert 't_pair(&t, "audio", 1)' in daemon
    assert 't_pair(&t, "pip"' not in daemon
    makefile = (ROOT / "amiga" / "Makefile.apollo").read_text(encoding="utf-8")
    assert "apollo_audio.o" in makefile
    assert "apollo.o" not in makefile and "ammx" not in makefile
