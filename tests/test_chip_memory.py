"""A missing Chip RAM reading stays missing. Zero stays zero.

An older daemon can omit chip_free. The viewer must not turn that into a
report of no free Chip RAM. Classic Amiga and ApolloOS use the same rule.
"""

import json
import shutil
import socket
import struct
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "amigent" / "web"


def test_a_real_chip_count_stays_distinct_from_a_missing_one():
    from amigent.viewer import counted_bytes, reported_bytes, status_free_bytes

    assert reported_bytes({"chip_free": "1887436"}, "chip_free") == 1887436
    assert reported_bytes({"chip_free": 1887436}, "chip_free") == 1887436
    assert reported_bytes({"chip_free": "0"}, "chip_free") == 0
    assert reported_bytes({"chip_free": 0}, "chip_free") == 0
    assert reported_bytes({"chip_free": " 0 "}, "chip_free") == 0
    assert reported_bytes({}, "chip_free") is None
    assert reported_bytes({"chip_free": ""}, "chip_free") is None
    assert reported_bytes({"chip_free": "   "}, "chip_free") is None
    assert reported_bytes({"chip_free": None}, "chip_free") is None
    assert reported_bytes({"chip_free": "nope"}, "chip_free") is None
    assert reported_bytes({"chip_free": "-1"}, "chip_free") is None
    assert reported_bytes({"chip_free": "1.5"}, "chip_free") is None
    assert counted_bytes({}, "fast_free") == 0
    assert status_free_bytes(None, 4096) == 4096
    assert status_free_bytes(0, 4096) == 4096
    assert status_free_bytes(1887436, 4096) == 1887436 + 4096


def _read(info: dict, system: dict):
    from amigent.viewer import Viewer

    viewer = Viewer.__new__(Viewer)
    viewer._system = system

    class Ctl:
        backend = type("B", (), {"protocol": 4})()

        def sysinfo(self):
            return info

        def volumes(self):
            return [{"name": "RAM:", "free": 1, "capacity": 2}]

    viewer.ctl = Ctl()
    viewer._ctl = lambda fn: fn()
    return viewer.read_amiga()


def test_read_amiga_keeps_zero_and_an_older_reply():
    classic = {"os": "AmigaOS 3.2", "cpu": "68030"}
    apollo = {"os": "ApolloOS", "cpu": "68080"}
    full = _read({
        "chip_free": "1887436", "chip_total": "2097152",
        "fast_free": "970981376", "fast_total": "1073741824",
        "bsdsocket": "4.307",
    }, classic)
    assert full["chip_free"] == 1887436 and full["fast_free"] == 970981376
    assert full["os"] == "AmigaOS 3.2"
    zero = _read({"chip_free": "0", "fast_free": "0"}, apollo)
    assert zero["chip_free"] == 0 and zero["fast_free"] == 0
    assert zero["os"] == "ApolloOS"
    older = _read({"fast_free": "4096", "bsdsocket": "4.1"}, classic)
    assert older["chip_free"] is None
    assert older["fast_free"] == 4096
    assert older["chip_total"] == 0 and older["fast_total"] == 0
    assert '"chip_free": null' in json.dumps({"chip_free": older["chip_free"]})


def test_an_older_daemon_omits_chip_free_and_zero_is_sent():
    from amigent.channel import SYSINFO, AmigaChannel

    assert SYSINFO == 30

    def ask(body: str) -> dict:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        raw = body.encode("iso-8859-1")
        packet = b"\0" + struct.pack(">I", len(raw)) + raw

        def serve():
            conn, _ = listener.accept()
            try:
                conn.sendall(b"AMGT" + struct.pack(">HH", 3, 0))
                rd = conn.makefile("rb")
                while len(head := rd.read(10)) == 10:
                    cmd = head[0]
                    if cmd == 20:
                        conn.sendall(b"\0")
                    elif cmd == SYSINFO:
                        conn.sendall(packet)
                    else:
                        conn.sendall(b"\1")
            finally:
                conn.close()

        threading.Thread(target=serve, daemon=True).start()
        port = listener.getsockname()[1]
        try:
            return AmigaChannel("127.0.0.1", port, timeout=5, key="").sysinfo()
        finally:
            listener.close()

    both = ask("chip_free=1887436\nfast_free=4096\n")
    assert both["chip_free"] == "1887436" and both["fast_free"] == "4096"
    zero = ask("chip_free=0\nfast_free=4096\n")
    assert zero["chip_free"] == "0"
    older = ask("fast_free=4096\nkickstart=47.115\n")
    assert "chip_free" not in older and older["fast_free"] == "4096"


def test_fleet_total_keeps_a_zero_and_ignores_a_missing_chip_count(monkeypatch):
    from amigent.fleet import Fleet

    fleet = Fleet.__new__(Fleet)
    fleet.entries = [{
        "info": {"os": "ApolloOS", "cpu": "68080", "chip_free": None, "fast_free": 4096},
        "name": "V4", "online": True, "seen": 1,
        "viewer": type("V", (), {"clients": set()})(),
    }]
    monkeypatch.setattr("amigent.fleet.machines.address", lambda host: host)
    assert fleet.status()[0]["free"] == 4096
    fleet.entries[0]["info"] = {"os": "AmigaOS", "cpu": "68030", "chip_free": 0, "fast_free": 4096}
    assert fleet.status()[0]["free"] == 4096
    fleet.entries[0]["info"] = {"os": "AmigaOS", "cpu": "68030", "chip_free": 1887436, "fast_free": 4096}
    assert fleet.status()[0]["free"] == 1887436 + 4096


def test_the_page_shows_zero_chip_and_hides_a_missing_reading():
    source = (WEB / "viewer.js").read_text(encoding="utf-8")
    known = source.split("function knownBytes", 1)[1].split("function bytes", 1)[0]
    program = "function knownBytes" + known + r"""
function show(info) {
  const chip = knownBytes(info.chip_free);
  const fast = knownBytes(info.fast_free) ?? 0;
  return {
    chip,
    memory: chip === null ? `Fast ${fast}` : `Chip ${chip}, Fast ${fast}`,
    total: (chip === null ? 0 : chip) + fast,
  };
}
console.log(JSON.stringify({
  positive: show({ chip_free: 1887436, fast_free: 4096, os: "AmigaOS" }),
  apollo: show({ chip_free: 1887436, fast_free: 4096, os: "ApolloOS" }),
  zero: show({ chip_free: 0, fast_free: 4096, os: "ApolloOS" }),
  stringZero: show({ chip_free: "0", fast_free: "4096", os: "AmigaOS" }),
  missing: show({ fast_free: 4096, os: "AmigaOS" }),
  blank: show({ chip_free: "", fast_free: 4096, os: "ApolloOS" }),
  invalid: show({ chip_free: "nope", fast_free: 4096, os: "AmigaOS" }),
}));
"""
    node = shutil.which("node") or shutil.which("node.exe")
    assert node, "node is required to check the viewer memory line"
    done = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    got = json.loads(done.stdout)
    assert got["positive"]["memory"] == "Chip 1887436, Fast 4096"
    assert got["apollo"] == got["positive"]
    assert got["zero"]["chip"] == 0 and got["zero"]["memory"] == "Chip 0, Fast 4096"
    assert got["zero"]["total"] == 4096
    assert got["stringZero"]["chip"] == 0
    assert got["missing"]["chip"] is None and got["missing"]["memory"] == "Fast 4096"
    assert got["blank"] == got["missing"]
    assert got["invalid"]["chip"] is None and got["invalid"]["total"] == 4096
    for path in (WEB / "locales").glob("*.json"):
        texts = json.loads(path.read_text(encoding="utf-8"))
        assert texts["mem_fast_only"].count("{fast}") == 1
        assert "{chip}" not in texts["mem_fast_only"]
        assert "{chip}" in texts["mem_line"] and "{fast}" in texts["mem_line"]
