"""TLSONLY refuses remote plaintext even when the TLS listener did not start."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAEMON = (ROOT / "amiga" / "amigentd.c").read_text(encoding="utf-8")
PROTOCOL = (ROOT / "docs" / "PROTOCOL.md").read_text(encoding="utf-8")


def accept_pending():
    start = DAEMON.index("static void accept_pending")
    end = DAEMON.index("static void start_channel", start)
    return DAEMON[start:end]


def remote_closed(tls_only, setup_mode, tls_listening, loopback):
    """The admission rule in accept_pending, including a listener that never opened.

    tls_listening is part of the case and must not change the result. The source
    is checked here so a return to the old tls_open condition fails these tests.
    """
    body = accept_pending()
    assert "locked = tls_only && !setup_mode;" in body
    assert "tls_only && tls_open" not in body
    assert "addr.sin_addr.s_addr != htonl(INADDR_LOOPBACK)" in body
    assert tls_listening in (True, False)
    locked = tls_only and not setup_mode
    return locked and not loopback


def test_tlsonly_with_a_listener_rejects_remote_plaintext():
    assert remote_closed(tls_only=True, setup_mode=False, tls_listening=True, loopback=False)


def test_tlsonly_without_a_listener_still_rejects_remote_plaintext():
    assert remote_closed(tls_only=True, setup_mode=False, tls_listening=False, loopback=False)


def test_tlsonly_keeps_loopback_open_for_the_tls_pump():
    assert not remote_closed(tls_only=True, setup_mode=False, tls_listening=False, loopback=True)
    assert not remote_closed(tls_only=True, setup_mode=False, tls_listening=True, loopback=True)


def test_setup_mode_keeps_remote_plaintext_open():
    assert not remote_closed(tls_only=True, setup_mode=True, tls_listening=False, loopback=False)
    assert not remote_closed(tls_only=True, setup_mode=True, tls_listening=True, loopback=False)


def test_without_tlsonly_remote_plaintext_stays_open():
    assert not remote_closed(tls_only=False, setup_mode=False, tls_listening=True, loopback=False)
    assert not remote_closed(tls_only=False, setup_mode=False, tls_listening=False, loopback=False)
    assert "plain port works as usual" not in PROTOCOL
    assert "whether or not the TLS port is listening" in PROTOCOL
