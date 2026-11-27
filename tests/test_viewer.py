import json
import os
import socket
import struct
import threading
import time

from amigent.viewer import Viewer, WebSocket, _amiga_name, _join, tiles_message
from test_amigentd import serve, tile


def test_accept_key_matches_rfc_example():
    assert WebSocket.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_tiles_message_layout():
    message = tiles_message([(64, 0, 2, 1, b"\x01\x02")])
    assert message == b"\x01" + struct.pack(">HHHHI", 64, 0, 2, 1, 2) + b"\x01\x02"


def test_amiga_names():
    assert _amiga_name("a/b:c.txt") == "a_b_c.txt"
    assert _amiga_name("Grüße€.lha") == "Grüße_.lha"
    assert _join("RAM:", "x") == "RAM:x" and _join("Work:Games", "x") == "Work:Games/x"


class Page:
    """A browser stand-in speaking just enough WebSocket."""

    def __init__(self, port, token):
        self.sock = socket.create_connection(("127.0.0.1", port), 5)
        self.sock.sendall(
            f"GET /ws?t={token} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.sock.recv(4096)
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        assert head.startswith(b"HTTP/1.1 101")

    def _exact(self, n):
        while len(self.buf) < n:
            self.buf += self.sock.recv(65536)
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def receive(self):
        b1, b2 = self._exact(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._exact(8))[0]
        data = self._exact(n)
        return data.decode() if b1 & 0x0F == 1 else data

    def send(self, op):
        data = json.dumps(op).encode()
        mask = os.urandom(4)
        self.sock.sendall(bytes([0x81, 0x80 | len(data)]) + mask
                          + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def test_page_gets_picture_and_drives_the_mouse():
    seen = []
    full = struct.pack(">I", 2) + tile(0, 0, 3) + tile(64, 0, 5)
    port = serve([full, struct.pack(">I", 0)], seen)
    viewer = Viewer("127.0.0.1", screen_port=port)
    viewer.start(0)
    page = Page(viewer.port, viewer.token)

    messages = []
    deadline = time.monotonic() + 5
    while not any(isinstance(m, bytes) and m[0] == 1 for m in messages):
        assert time.monotonic() < deadline
        messages.append(page.receive())
    hello = json.loads(messages[0])
    assert hello["type"] == "hello"
    tiles = next(m for m in messages if isinstance(m, bytes))
    assert struct.unpack(">HHHHI", tiles[1:13])[:4] == (0, 0, 64, 64)

    page.send({"t": "move", "x": 100, "y": 20})
    page.send({"t": "buttons", "mask": 1})
    deadline = time.monotonic() + 5
    while not any(c[0] == 4 for c in seen):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    move = next(c for c in seen if c[0] == 3)
    assert struct.unpack(">HH", move[2:6]) == (100, 20)
    assert next(c for c in seen if c[0] == 4)[1] == 1


def test_wrong_token_is_refused():
    viewer = Viewer("127.0.0.1", screen_port=1)
    viewer.start(0)
    sock = socket.create_connection(("127.0.0.1", viewer.port), 5)
    sock.sendall(f"GET /?t=nope HTTP/1.1\r\nHost: 127.0.0.1:{viewer.port}\r\n\r\n".encode())
    assert sock.recv(100).startswith(b"HTTP/1.1 403")


def test_foreign_host_header_is_refused():
    viewer = Viewer("127.0.0.1", screen_port=1)
    viewer.start(0)
    sock = socket.create_connection(("127.0.0.1", viewer.port), 5)
    sock.sendall(f"GET /?t={viewer.token} HTTP/1.1\r\nHost: evil.example:{viewer.port}\r\n\r\n".encode())
    assert sock.recv(100).startswith(b"HTTP/1.1 403")


def test_windows_launcher_quotes_for_vbs(monkeypatch):
    from amigent import desktop

    monkeypatch.setattr(desktop, "_wsl", lambda: True)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Debian")
    monkeypatch.setenv("AMIGENT_FLUSH_VOLUMES", "Work: SYS:")
    name, text = desktop._launcher("192.168.1.50")
    assert name == "Amigent.vbs"
    assert text.startswith('CreateObject("WScript.Shell").Run "wsl.exe -d Debian ')
    assert '""AMIGENT_FLUSH_VOLUMES=Work: SYS:""' in text
    assert text.rstrip().endswith('192.168.1.50 --exit-when-closed", 0, False')


def test_no_running_viewer_without_state(monkeypatch, tmp_path):
    import amigent.viewer as viewer

    monkeypatch.setattr(viewer, "STATE", tmp_path / "viewer.json")
    assert viewer.running_viewer("192.168.1.50") is None


# Texts of the 1.5.0 redesign that wait for their translation: English fills in until then.
# Every other text must be in every language; German is always complete.
AWAITING_TRANSLATION: set[str] = set()  # keys added in English that other languages still lack


def test_every_language_has_every_text():
    import re
    from pathlib import Path

    web = Path(__file__).resolve().parents[1] / "src" / "amigent" / "web"
    english = json.loads((web / "locales" / "en.json").read_text(encoding="utf-8"))
    used = set()
    for page in ("index.html", "fleet.html"):
        used |= set(re.findall(r'data-i18n(?:-title|-label|-aria|-placeholder)?="([a-z_]+)"',
                               (web / page).read_text(encoding="utf-8")))
    for script in ("ui.js", "viewer.js", "machines.js", "fleet.js"):
        used |= set(re.findall(r'\bt\("([a-z_]+)"', (web / script).read_text(encoding="utf-8")))
    used |= {"restarting", "restart_failed", "not_supported", "pause_failed"}  # sent by the server
    used |= {f"theme_{name}" for name in ("auto", "light", "dark")}
    used |= {f"machines_{action}" for action in ("connect", "rename", "default", "address", "remove")}
    used |= {f"fpu_{state}" for state in ("none", "chip", "fpu", "loaded", "missing", "emu68")}  # Fpu.state()
    assert used <= english.keys(), used - english.keys()
    script = (web / "ui.js").read_text(encoding="utf-8")
    codes = set(re.findall(r"\b([a-z]{2}): \"", script.split("const LANGUAGES")[1].split("};")[0]))
    assert {"en", "de", "ru"} <= codes
    assert AWAITING_TRANSLATION <= english.keys(), AWAITING_TRANSLATION - english.keys()
    for code in codes:
        texts = json.loads((web / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        assert texts.keys() <= english.keys(), (code, texts.keys() - english.keys())
        missing = english.keys() - texts.keys()
        if code == "de":
            assert not missing, missing
        assert missing <= AWAITING_TRANSLATION, (code, missing - AWAITING_TRANSLATION)
        for key, text in texts.items():
            assert set(re.findall(r"{\w+}", text)) == set(re.findall(r"{\w+}", english[key])), (code, key)


def test_locale_catalogs_have_no_duplicate_keys():
    import re
    from pathlib import Path

    web = Path(__file__).resolve().parents[1] / "src" / "amigent" / "web"
    folder = web / "locales"
    for path in sorted(folder.glob("*.json")):
        seen = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith('"'):
                continue
            seen.append(json.loads(stripped.split('":', 1)[0] + '"'))
        assert len(seen) == len(set(seen)), path.name
        json.loads(path.read_text(encoding="utf-8"))
    shared = (web / "ui.js").read_text(encoding="utf-8")  # the language list every page uses
    codes = re.findall(r"\b([a-z]{2}): \"", shared.split("const LANGUAGES", 1)[1].split("};", 1)[0])
    assert "ru" in codes
    for code in codes:
        assert (folder / f"{code}.json").is_file(), code


def test_describe_system():
    from amigent.viewer import describe_system, exec_before_os3

    facts = describe_system("Kickstart 47.115, Workbench 47.5\n",
                            "System: 68040 68882   (INST: Cache Burst) (DATA: Cache Burst)\n")
    assert facts == {"os": "AmigaOS 3.2", "kickstart": "47.115", "workbench": "47.5",
                     "cpu": "68040", "fpu": "68882"}
    assert describe_system("", "")["os"] == ""
    assert exec_before_os3("37.175") and not exec_before_os3("39.106")
    assert not exec_before_os3("") and not exec_before_os3("Apollo")


def _restart_of(stat, info, online=True, kickstart=""):
    from amigent.errors import AmigaError

    viewer = Viewer("127.0.0.1", screen_port=1)
    viewer.online = online
    viewer.amiga = {"kickstart": kickstart}
    viewer.flush_volumes = ["SYS:"]
    seen = {"restart": 0, "flush": None, "ring": 0, "toasts": []}

    class Ctl:
        def stat(self, path):
            assert path == "C:Reboot"
            if isinstance(stat, AmigaError):
                raise stat
            return {"type": "file"}

        def sysinfo(self):
            return info

        def flush(self, volumes):
            seen["flush"] = list(volumes)

    viewer.ctl = Ctl()
    viewer._ctl = lambda fn: fn()
    viewer.restart = lambda: seen.__setitem__("restart", seen["restart"] + 1)
    viewer.feed.ring = lambda: seen.__setitem__("ring", seen["ring"] + 1)
    viewer.event = lambda kind, **fields: seen["toasts"].append((kind, fields))
    viewer._restart()
    return viewer, seen


def test_os2_without_reboot_submits_the_reset_chord():
    from amigent.amigentd import reset_chord
    from amigent.errors import AmigaError

    viewer, seen = _restart_of(AmigaError("object not found", 205), {"exec_version": "37.175"},
                               kickstart="40.68")
    assert seen["restart"] == 0
    assert seen["flush"] == ["SYS:"] and seen["ring"] == 1
    assert viewer.inputs.take() == reset_chord()
    assert seen["toasts"] == [("toast", {"key": "restarting"})]


def test_os2_uses_kickstart_when_sysinfo_has_no_exec():
    from amigent.errors import AmigaError

    viewer, seen = _restart_of(AmigaError("object not found", 205), {}, kickstart="37.175")
    assert seen["restart"] == 0 and seen["ring"] == 1
    assert [op["code"] for op in viewer.inputs.take()] == [0x63, 0x66, 0x67]


def test_os3_without_reboot_still_uses_the_command_path():
    from amigent.errors import AmigaError

    viewer, seen = _restart_of(AmigaError("object not found", 205), {"exec_version": "40.68"},
                               kickstart="37.175")
    assert seen["restart"] == 1 and seen["flush"] is None and seen["ring"] == 0
    assert viewer.inputs.take() == []


def test_reboot_present_on_os2_is_still_the_command():
    viewer, seen = _restart_of({"type": "file"}, {"exec_version": "37.175"})
    assert seen["restart"] == 1 and seen["ring"] == 0
    assert viewer.inputs.take() == []


def test_reset_chord_waits_until_the_amiga_is_connected():
    from amigent.errors import AmigaError

    viewer, seen = _restart_of(AmigaError("object not found", 205), {"exec_version": "37.175"},
                               online=False)
    assert seen["flush"] is None and seen["ring"] == 0 and seen["restart"] == 0
    assert viewer.inputs.take() == []
    assert seen["toasts"][-1] == ("toast", {"key": "restart_failed", "text": "the Amiga is not connected"})


def test_exec_below_36_is_not_sent_the_reset_chord():
    from amigent.errors import AmigaError

    for version in ("34.5", "35.0"):
        viewer, seen = _restart_of(AmigaError("object not found", 205), {"exec_version": version})
        assert seen["flush"] is None and seen["ring"] == 0 and seen["restart"] == 0
        assert viewer.inputs.take() == []
        text = seen["toasts"][-1][1]["text"]
        assert seen["toasts"][-1][1]["key"] == "restart_failed"
        assert "cannot ColdReboot()" in text and version in text


def test_exec_36_without_reboot_still_submits_the_reset_chord():
    from amigent.amigentd import reset_chord
    from amigent.errors import AmigaError

    viewer, seen = _restart_of(AmigaError("object not found", 205), {"exec_version": "36.16"})
    assert seen["restart"] == 0
    assert seen["flush"] == ["SYS:"] and seen["ring"] == 1
    assert viewer.inputs.take() == reset_chord()


def test_kickstart_below_36_is_not_sent_the_reset_chord():
    from amigent.errors import AmigaError

    viewer, seen = _restart_of(AmigaError("object not found", 205), {}, kickstart="34.1")
    assert seen["flush"] is None and seen["ring"] == 0 and seen["restart"] == 0
    assert viewer.inputs.take() == []
    assert "34.1" in seen["toasts"][-1][1]["text"]


def test_an_ordinary_key_is_not_the_reset_chord():
    viewer = Viewer("127.0.0.1", screen_port=1)
    viewer.feed.ring = lambda: None
    key = {"t": "key", "code": 0x45, "down": True, "q": 0}
    viewer.submit(key)
    assert viewer.inputs.take() == [key]


def test_a_lost_connection_is_not_a_missing_reboot():
    from amigent.errors import AmigaError

    viewer, seen = _restart_of(AmigaError("connection closed"), {"exec_version": "37.175"})
    assert seen["ring"] == 0 and seen["restart"] == 0
    assert viewer.inputs.take() == []
    assert seen["toasts"][-1][1]["key"] == "restart_failed"


def test_mac_app_bundle(monkeypatch, tmp_path):
    from amigent import desktop

    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop, "MAC_APP", tmp_path / "Amigent.app")
    monkeypatch.setattr(desktop, "MAC_CONFIG", tmp_path / "app.json")
    monkeypatch.setattr(desktop, "MAC_RELEASE", tmp_path / "none.zip")
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)  # no Xcode tools here
    monkeypatch.setenv("AMIGENT_FLUSH_VOLUMES", "Work: SYS:")
    made = desktop.install("A1200")
    app = tmp_path / "Amigent.app" / "Contents"
    assert made == [str(tmp_path / "Amigent.app")]
    script = (app / "MacOS" / "Amigent").read_text()
    assert "amigent.viewer A1200 --exit-when-closed" in script
    assert "export AMIGENT_FLUSH_VOLUMES='Work: SYS:'" in script
    plist = (app / "Info.plist").read_text()
    assert (app / "Resources" / "amigent.icns").exists() and "<string>Amigent</string>" in plist
    assert "NSLocalNetworkUsageDescription" in plist
    config = json.loads((tmp_path / "app.json").read_text())
    assert config["args"] == ["A1200"] and config["env"]["AMIGENT_FLUSH_VOLUMES"] == "Work: SYS:"
    assert desktop.uninstall() == [str(tmp_path / "Amigent.app"), str(tmp_path / "app.json")]


def test_clipboard_text_round_trip():
    from amigent.viewer import ftxt, ftxt_text

    data = ftxt("Grüße\r\nvom PC")
    assert data[:4] == b"FORM" and data[8:12] == b"FTXT" and len(data) % 2 == 0
    assert ftxt_text(data) == "Grüße\nvom PC"
    assert ftxt_text(b"FORM\0\0\0\4ILBM") == ""


def test_powerpc_systems_are_named():
    from amigent.system import describe_system, native_ppc

    os4 = describe_system("Kickstart 53.73, Workbench 53.12\n", "CPU: PowerPC 7447/7457 G4\n")
    assert os4["os"] == "AmigaOS 4" and os4["cpu"].startswith("PowerPC 7447")
    assert native_ppc(os4)
    named = describe_system("AmigaOS 4.1 Final Edition Update 2\nKickstart 54.30\n", "")
    assert named["os"] == "AmigaOS 4.1"
    mos = describe_system("MorphOS version 3.18\nKickstart 51.37\n", "")
    assert mos["os"] == "MorphOS 3.18" and native_ppc(mos)
    assert not native_ppc(describe_system("Kickstart 47.115, Workbench 47.5\n", "System: 68040"))


def test_ppc_software_reads_only_version_strings():
    from amigent.system import ppc_software

    asked = []

    def run(command):
        asked.append(command)
        if "powerpc.library" in command:
            return 0, "powerpc.library 17.10\n"
        return 20, "Version: object not found\n"

    assert ppc_software(run) == "WarpOS powerpc.library 17.10"
    assert all(c.startswith("Version FILE ") for c in asked)
    assert ppc_software(lambda c: (20, "")) == ""


def test_connection_reason_names_a_known_failure_and_drops_the_detail():
    from amigent import tls
    from amigent.viewer import connection_reason

    assert connection_reason("amigentd at 10.0.0.5:6801 not answering: timed out") == "reason_timeout"
    refused = ("amigentd at 10.0.0.5:6801 not answering: [WinError 10061] "
               "No connection could be made because the target machine actively refused it")
    assert connection_reason(refused) == "reason_refused"
    windows_timeout = ("amigentd at amiga:6801 not answering: [WinError 10060] A connection "
                       "attempt failed because the connected party did not properly respond "
                       "after a period of time, or established connection failed because "
                       "connected host has failed to respond")
    assert connection_reason(windows_timeout) == "reason_timeout"
    assert connection_reason("connection closed by amigentd") == "reason_closed_by_daemon"
    assert connection_reason("amigentd stopped answering") == "reason_stopped_answering"
    assert connection_reason("unexpected greeting b'HTTP/1.1 secret-key'") == "reason_unexpected_greeting"
    assert connection_reason("refused the key hunter2") == "reason_key_refused"
    assert connection_reason(tls.PLAIN_CLOSED_TEXT) == "reason_plain_closed"
    assert connection_reason(tls.NO_TLS_TEXT) == "reason_no_tls"
    mismatch = ("amigentd at 10.0.0.5:6801 not answering: [SSL: CERTIFICATE_VERIFY_FAILED] "
                "certificate verify failed: self-signed certificate (_ssl.c:1006)")
    assert connection_reason(mismatch) == "reason_tls_mismatch"
    ambiguous = "Ambiguous TLS pin for 10.0.0.5. Save its verified certificate as tls-amiga-1.pem."
    assert connection_reason(ambiguous) == "reason_pin_ambiguous"
    leaked = "amigentd at 10.1.2.3:6801 not answering: [Errno 13] Permission denied: '/home/x/secret'"
    assert connection_reason(leaked) == ""
    assert connection_reason("<img src=x onerror=alert(1)>") == ""
    assert connection_reason("AMIGENT_KEY=supersecret") == ""
    assert connection_reason("") == ""
    assert connection_reason("tile at 1,2: bad packed data") == ""


def test_set_online_sends_a_key_to_the_page_and_the_raw_reason_to_the_terminal(capsys, monkeypatch):
    from amigent.viewer import Viewer

    monkeypatch.setattr("amigent.viewer.machines.name_of", lambda host: host)
    viewer = Viewer.__new__(Viewer)
    viewer.online = None
    viewer.reason = ""
    viewer.host = "amiga.local"
    viewer.clients_lock, viewer.hearers, viewer._media_wake = threading.Lock(), set(), threading.Event()
    events = []
    viewer.event = lambda kind, **fields: events.append((kind, fields))
    viewer._set_online(False, "amigentd at 10.0.0.8:6801 not answering: timed out")
    assert events == [("online", {"online": False, "reason": "reason_timeout"})]
    # the user's own terminal gets the whole text, address included
    assert "the Amiga does not answer: amigentd at 10.0.0.8:6801 not answering: timed out" in \
        capsys.readouterr().err
    viewer._set_online(False, "amigentd at 10.0.0.8:6801 not answering: timed out")
    assert len(events) == 1
    viewer._set_online(False, "amigentd at 10.0.0.8:6801 not answering: connection refused")
    assert events[-1][1] == {"online": False, "reason": "reason_refused"}
    viewer._set_online(False, "tile decoder blew up at /tmp/secret")
    assert events[-1][1]["reason"] == ""
    assert "tile decoder blew up" in capsys.readouterr().err
    viewer._set_online(True)
    assert events[-1] == ("online", {"online": True, "reason": ""})
    assert "connected to amiga.local" in capsys.readouterr().err
    viewer._set_online(True)
    assert len(events) == 4


def test_every_reason_key_has_its_text_in_every_language():
    from pathlib import Path

    from amigent.viewer import CONNECTION_REASONS

    locales = Path(__file__).resolve().parents[1] / "src" / "amigent" / "web" / "locales"
    english = json.loads((locales / "en.json").read_text(encoding="utf-8"))
    assert {k: english[k] for k in CONNECTION_REASONS} == CONNECTION_REASONS
    for path in locales.glob("*.json"):
        texts = json.loads(path.read_text(encoding="utf-8"))
        assert all(texts.get(k) for k in CONNECTION_REASONS), path.name


def test_page_shows_only_known_link_reasons():
    from pathlib import Path

    web = Path(__file__).resolve().parents[1] / "src" / "amigent" / "web"
    script = (web / "viewer.js").read_text(encoding="utf-8")
    shown = script.split("function shownLinkReason(key)", 1)[1].split("}", 1)[0]
    assert "/^reason_[a-z_]+$/.test(key)" in shown and "UI.knows(key)" in shown
    knows = (web / "ui.js").read_text(encoding="utf-8").split("function knows(key)", 1)[1].split("}", 1)[0]
    assert "Object.hasOwn(EN, key)" in knows
    show = script.split("function showState()", 1)[1].split("function showPaused(", 1)[0]
    assert "shownLinkReason(reason)" in show
    assert '$("overlay-text").textContent = message' in show
    assert 't("offline")' in show
    assert "innerHTML" not in show
