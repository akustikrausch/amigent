"""1.3.0 part two: packing, screen text, waiting, trees, debug output, notes,
installations that can be undone, Aminet, and several steps in one call."""

import json
import shutil
import socket
import struct
import subprocess
import threading
import zlib

import pytest

import amigent.server as server
from amigent import aminet, install, lzh, notes, pack, screentext
from amigent.channel import (COMPRESS, DEBUGLOG, READ, SYSINFO, TEXT, TREE, WAIT, WRITE,
                             AmigaChannel, parse_tree, unpack_frames)
from amigent.errors import AmigaError

# ---- a protocol 5 channel on the PC


class Fake5:
    """Speaks enough of amigentd's protocol 5 command channel for the client."""

    def __init__(self, rates=(20000, 9000, 20000), files=None, text_lines=None, notes=True):
        self.rates = rates
        self.notes = notes
        self.files = dict(files or {})
        self.text_lines = text_lines or []
        self.level = 0
        self.seen = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _exact(self, conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def _blob(self, conn, data: bytes):
        conn.sendall(b"\0" + struct.pack(">I", len(data)))
        if not self.level:
            conn.sendall(data)
            return
        packer = zlib.compressobj(self.level, zlib.DEFLATED, -15)
        for at in range(0, max(len(data), 1), 1000):
            part = packer.compress(data[at:at + 1000])
            if part:
                conn.sendall(struct.pack(">I", len(part)) + part)
        tail = packer.flush()
        conn.sendall(struct.pack(">I", len(tail)) + tail + b"\0\0\0\0")

    def _serve(self):
        while True:
            conn, _ = self.listener.accept()
            self.level = 0
            conn.sendall(b"AMGT" + struct.pack(">HH", 5, 0))
            try:
                while True:
                    cmd, arg, x, y, w, h = struct.unpack(">BBHHHH", self._exact(conn, 10))
                    path = self._exact(conn, x).decode("latin-1") if cmd not in (SYSINFO, DEBUGLOG, COMPRESS, TEXT) else ""
                    self.seen.append((cmd, arg, path))
                    if cmd == 20:
                        conn.sendall(b"\0")
                    elif cmd == 37:  # NOTE: no answer
                        pass
                    elif cmd == SYSINFO:
                        lines = (f"cpu=68040\ndeflate_fast={self.rates[0]}\ndeflate_best={self.rates[1]}\n"
                                 f"inflate={self.rates[2]}\ntext=1\ndebug=1\n"
                                 + ("note=1\n" if self.notes else ""))
                        self._blob(conn, lines.encode())
                    elif cmd == COMPRESS:
                        self.level = arg
                        conn.sendall(b"\0")
                    elif cmd == READ:
                        self._blob(conn, self.files[path])
                    elif cmd == WRITE:
                        data = self._exact(conn, w << 16 | h)
                        self.files[path] = zlib.decompress(data, -15) if arg & 2 else data
                        conn.sendall(b"\0")
                    elif cmd == TEXT:
                        self._blob(conn, "".join(l + "\n" for l in self.text_lines).encode("latin-1"))
                    elif cmd == WAIT:
                        if path == "never":
                            conn.sendall(b"\2" + struct.pack(">H", 7) + b"timeout")
                        else:
                            self._blob(conn, f"W\t0000abcd\t1\t2\t3\t4\ta\t{path}\n".encode())
                    elif cmd == TREE:
                        self._blob(conn, b"DIR\tsub\t0\t00000000\t2026-11-06 10:00:00\n"
                                         b"FILE\tsub/a\t3\t00000000\t2026-11-06 10:00:00\t0000abcd\n")
                    elif cmd == DEBUGLOG:
                        since = w << 16 | h
                        log = b"hello\n"
                        part = log[since:] if since <= len(log) else log
                        conn.sendall(b"\0" + struct.pack(">II", len(log), len(part)) + part)
                    else:
                        conn.sendall(b"\1" + struct.pack(">H", 4) + b"what")
            except (EOFError, OSError):
                conn.close()


def channel_to(fake, mode="auto", line=None):
    ch = AmigaChannel("127.0.0.1", fake.port, timeout=5, key="")
    ch.policy = pack.Policy("test", "10.0.0.9", mode=mode)
    ch.policy.line_kbs = line
    ch.connect()
    return ch


def test_a_fast_amiga_on_a_slow_line_packs_its_replies():
    fake = Fake5(files={"S:big": b"Workbench " * 5000})
    ch = channel_to(fake, line=40.0)
    assert ch.policy.level() == 2 and ch.policy.upload()
    assert ch.read_file("S:big") == b"Workbench " * 5000
    assert ch.zlevel == 2 and (COMPRESS, 2, "") in fake.seen


def test_a_slow_amiga_on_a_fast_line_does_not():
    fake = Fake5(rates=(150, 60, 400), files={"S:big": b"x" * 20000})
    ch = channel_to(fake, line=900.0)
    assert ch.policy.level() == 0 and not ch.policy.upload()
    assert ch.read_file("S:big") == b"x" * 20000
    assert not any(c == COMPRESS for c, _, _ in fake.seen)


def test_packed_writes_arrive_whole():
    fake = Fake5()
    ch = channel_to(fake, mode="best")
    text = "".join(f"line {i}\n" for i in range(3000)).encode()
    ch.write_file("RAM:t", text)
    assert fake.files["RAM:t"] == text
    assert any(c == WRITE and a & 2 for c, a, _ in fake.seen)
    noise = bytes(range(256)) * 2  # does not pack by a tenth: goes as it is
    ch.write_file("RAM:n", b"ab" * 100)
    assert fake.files["RAM:n"] == b"ab" * 100
    del noise


def test_mode_off_by_hand_wins():
    fake = Fake5(files={"S:f": b"y" * 10000})
    ch = channel_to(fake, mode="off", line=10.0)
    assert ch.read_file("S:f") == b"y" * 10000 and ch.zlevel == 0


def test_text_wait_tree_and_debug_log():
    fake = Fake5(text_lines=["I\t5\t0\t2\t4096", "W\t0000abcd\t0\t0\t100\t50\ta\tShell",
                             "L\t0000abcd\t5\t20\t50\t8\thello"])
    ch = channel_to(fake)
    assert ch.screen_text()[2].endswith("hello")
    assert ch.wait_for(1, "Shell", 5)[0].endswith("Shell")
    with pytest.raises(AmigaError) as e:
        ch.wait_for(1, "never", 2)
    assert e.value.code == 2
    tree = ch.tree("SYS:x", crc=True)
    assert tree[1] == {"type": "FILE", "name": "sub/a", "size": 3, "protection": "00000000",
                       "date": "2026-11-06 10:00:00", "crc": 0xABCD}
    assert ch.debug_log(0) == (6, b"hello\n")
    assert ch.debug_log(6) == (6, b"")


def test_older_amigentd_says_so():
    from test_protocol2 import fake_amigentd  # noqa: F401  (protocol 3 hello below)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def run():
        conn, _ = listener.accept()
        conn.sendall(b"AMGT" + struct.pack(">HH", 3, 0))
        conn.recv(10)
        conn.sendall(b"\0")
        conn.recv(10)

    threading.Thread(target=run, daemon=True).start()
    ch = AmigaChannel("127.0.0.1", listener.getsockname()[1], timeout=5, key="")
    ch.connect()
    with pytest.raises(AmigaError, match="1.3.0"):
        ch.screen_text()


def test_frames_unpack_and_count_the_wire():
    data = b"Amiga " * 4000
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    body = packer.compress(data) + packer.flush()
    wire = struct.pack(">I", len(body)) + body + b"\0\0\0\0"
    view = memoryview(wire)
    at = [0]

    def read(n):
        part = bytes(view[at[0]:at[0] + n])
        at[0] += n
        return part

    out, count = unpack_frames(read, len(data))
    assert out == data and count == len(wire)


def test_tree_lines():
    got = parse_tree(["FILE\ta b\t9\t0000000f\t2026-01-01 00:00:00\t-", "ERR\tsub\t218", "MORE"])
    assert got[0]["crc"] is None and got[1]["type"] == "ERR" and got[2] == {"type": "MORE"}


# ---- the packing policy


def test_policy_levels_follow_the_ratio(tmp_path):
    p = pack.Policy("A", "10.0.0.2", path=tmp_path / "l.json")
    p.learn({"deflate_fast": "300", "deflate_best": "150", "inflate": "600"})
    p.line_kbs = 40
    assert p.level() == 2  # 150 >= 3 * 40
    p.line_kbs = 60
    assert p.level() == 1  # 150 < 180, 300 >= 120
    p.line_kbs = 200
    assert p.level() == 0 and p.upload()  # unpacking is cheaper: 600 >= 2 * 200
    p.line_kbs = 400
    assert not p.upload()
    p.set_mode("fast")
    assert p.level() == 1 and p.upload()
    again = pack.Policy("A", "10.0.0.2", path=tmp_path / "l.json")
    assert again.mode == "fast"  # kept for this Amiga


def test_line_is_measured_and_kept(tmp_path):
    p = pack.Policy("B", "10.0.0.3", path=tmp_path / "l.json")
    p.note(100, 1.0)  # too small to tell
    assert p.line_kbs is None
    p.note(64 * 1024, 2.0)
    assert round(p.line_kbs) == 32
    assert round(pack.Policy("B", "10.0.0.3", path=tmp_path / "l.json").line_kbs) == 32


def test_unknown_rates_never_pack(tmp_path):
    p = pack.Policy("C", "127.0.0.1", path=tmp_path / "l.json")
    assert p.level() == 0 and p.line() == pack.LOCAL_LINE


# ---- screen text

LINES = [
    "I\t30\t0\t5\t4096",
    "W\t00000001\t100\t100\t300\t200\ta\tFront",
    "L\t00000001\t110\t120\t80\t10\tSpeichern",
    "W\t00000002\t0\t0\t640\t480\t\tBack",
    "L\t00000002\t150\t150\t60\t10\tcovered",
    "L\t00000002\t10\t10\t60\t10\tvisible",
    "W\t00000003\t500\t0\t100\t100\to\tOld",
    "W\t00000004\t0\t0\t640\t480\tb\t",
]


def test_rows_behind_a_window_in_front_are_left_out():
    st = screentext.parse(LINES)
    text = screentext.render(st)
    assert "visible" in text and "covered" not in text and "1 row(s) behind" in text
    assert "Old" in text and "open before" in text
    assert "covered" in screentext.render(st, hidden=True)


def test_find_window_and_region():
    st = screentext.parse(LINES)
    assert "Speichern" in screentext.render(st, find="speich")
    assert "is not in" in screentext.render(st, find="nowhere")
    assert "Front" not in screentext.render(st, window="Back")
    only = screentext.render(st, region=(0, 0, 100, 100))
    assert "visible" in only and "Speichern" not in only


def test_no_recording_says_take_a_picture():
    assert "amiga_screenshot" in screentext.render(screentext.parse(["I\t-1\t0\t0\t0"]))


def test_click_point_lies_on_the_word():
    st = screentext.parse(["I\t1\t0\t1\t10", "W\t1\t0\t0\t400\t100\ta\tW",
                           "L\t1\t0\t0\t200\t10\tOK      Cancel"])
    win, row = screentext.find_row(st, "cancel")
    x, y = screentext.point_of(row, "Cancel")
    assert 140 < x < 200 and y == 5


# ---- notes


def test_notes_are_kept_per_topic(tmp_path):
    book = notes.Notes(tmp_path / "n.json")
    book.add("settlers2", "needs Stack 2000000")
    book.add("settlers2", "needs Stack 2000000")  # once is enough
    book.add("protracker", "audio menu")
    assert book.get("Settlers2") == ["needs Stack 2000000"]
    assert book.topics() == {"protracker": 1, "settlers2": 1}
    assert "settlers2" in book.search("stack")
    assert book.remove("settlers2", "") == 1 and not book.get("settlers2")
    assert notes.topic_of('SYS:Games/Settlers2/settlers2 >NIL:') == "settlers2"
    assert notes.topic_of('"Work:My Prog/prog.exe"') == "prog"


def test_start_shows_the_notes(monkeypatch):
    monkeypatch.setattr(server, "_clear_hold", lambda: "")
    monkeypatch.setattr(server, "_with_ctl", lambda fn: None)
    monkeypatch.setattr(server, "_window_ids", lambda: [])
    notes.for_amiga(server._amiga()).add("clock", "opens on Workbench")
    said = server.amiga_start("SYS:Utilities/Clock")
    assert "opens on Workbench" in said


# ---- installations


def entry(kind, size=1, crc=1, date="d"):
    return {"type": kind, "size": size, "crc": crc, "date": date}


def test_compare_tells_added_changed_removed():
    before = {"a": entry("FILE"), "b": entry("FILE"), "d": entry("DIR")}
    after = {"a": entry("FILE"), "b": entry("FILE", crc=2), "c": entry("FILE"), "d": entry("DIR")}
    assert install.compare(before, after) == (["c"], ["b"], [])
    assert install.compare(after, before) == ([], ["b"], ["c"])
    assert install.compare(None, {"x": entry("FILE")}) == (["x"], [], [])


def test_scripts_name_the_right_paths():
    r = install.Record("demo", "Stuff:amigent", ["C:", "SYS:WBStartup"])
    r.before = {"C:": {"Dir": entry("FILE"), "New": entry("FILE")}, "SYS:WBStartup": {}}
    r.added = ["C:Foo", "SYS:WBStartup/Bar"]
    r.changed = ["C:Dir"]
    assert 'Copy "C:" "Stuff:amigent/undo/demo/C" ALL CLONE QUIET' in install.begin_script(r)
    prune = install.prune_script(r)
    assert "undo/demo/C/New" in prune and "undo/demo/C/Dir" not in prune
    undo = install.undo_script(r, [])
    assert 'Delete >NIL: "C:Foo" QUIET' in undo
    assert 'Copy "Stuff:amigent/undo/demo/C/Dir" "C:Dir" CLONE QUIET' in undo
    entries = {"C:Foo": entry("FILE", size=12), "SYS:WBStartup/Bar": entry("FILE", size=3),
               "C:Dir": entry("FILE", size=99)}
    module = install.repair_module(r, entries)
    assert 'If NOT $amigentsize EQ 12 VAL' in module and "apps/demo/C_Foo" in module
    assert '"Stuff:amigent/apps/demo/C_Dir" "C:Dir"' in module  # a changed file comes back too
    r.added += ["SYS:Games/New", "SYS:Games/New/a", "SYS:Games/New/sub", "SYS:Games/New/sub/b"]
    entries.update({"SYS:Games/New": entry("DIR"), "SYS:Games/New/a": entry("FILE"),
                    "SYS:Games/New/sub": entry("DIR"), "SYS:Games/New/sub/b": entry("FILE")})
    module = install.repair_module(r, entries)
    assert module.count("SYS_Games_New") == 1 and "ALL CLONE" in module  # one block, the folder
    assert "SYS:Games/New/a" not in module
    with pytest.raises(AmigaError):
        install.name_ok("bad:name")


class TreeCtl:
    """ctl for the install tools: TREE answers from a dict, the rest is recorded."""

    def __init__(self, trees):
        self.trees, self.calls, self.written = trees, [], {}

    def tree(self, path, crc=False):
        if path not in self.trees:
            raise AmigaError("not found", 1)
        return [{"name": n, **e} for n, e in self.trees[path].items()]

    def write_file(self, path, data):
        self.written[path] = data

    def exec(self, line, cd=None, timeout=None):
        self.calls.append(line)
        from amigent.client import ExecResult
        return ExecResult(0, "")

    def stat(self, path):
        return {"type": "file"}

    def delete(self, path):
        self.calls.append(f"delete {path}")

    def makedir(self, path):
        pass

    def list_dir(self, path):
        return []


def test_install_round_trip(monkeypatch):
    trees = {"C:": {"Dir": entry("FILE")}, "LIBS:": {}}
    fake = TreeCtl(trees)
    monkeypatch.setattr(server, "ctl", fake)
    monkeypatch.setattr(server, "_with_ctl", lambda fn: fn())
    monkeypatch.setattr(install, "SYSTEM", ["C:", "LIBS:"])
    monkeypatch.setattr(server.files, "makedirs", lambda ctl, path: None)
    assert "ready" in server.amiga_install_begin("demo")
    trees["C:"]["Foo"] = entry("FILE")
    trees["LIBS:"]["x.library"] = entry("FILE")
    said = server.amiga_install_end("demo")
    assert "2 added, 0 changed, 0 removed" in said
    assert any(p.endswith("repair.d/demo.repair") for p in fake.written)
    del trees["C:"]["Foo"], trees["LIBS:"]["x.library"]  # what the undo script does
    assert "taken back" in server.amiga_install_undo("demo")
    assert "undone" in server.amiga_installs()


# ---- Aminet

PAGE = """<table><tr class="lightrow pkg_row">
  <td nowrap class="name_col"><font size="2"><a href="/game/edit/EdSet08.lha">EdSet08.lha</a></font></td>
  <td><font size="2"></font></td>
  <td nowrap><font size="2"><a href="/game/edit">game/edit</a></font></td>
  <td><font size="2">9448</font></td>
  <td class="size_col"><font size="2">69K</font></td>
  <td><font size="2">1994-07-21</font></td>
  <td><font size="2"><a href="//m68k.aminet.net"><img alt="m68k-amigaos icon" src="/x.png"></a></font></td>
  <td><font size="2"><a href="/package/game/edit/EdSet08">Save file editor for &quot;The Settlers&quot;</a> - <a href="/game/edit/EdSet08.readme">(readme)</a></font></td>
</tr></table>"""


def test_search_page_rows():
    [p] = aminet.parse_search(PAGE)
    assert p.path == "game/edit/EdSet08.lha" and p.downloads == 9448 and p.arch == "m68k-amigaos"
    assert p.description == 'Save file editor for "The Settlers"' and p.readme == "game/edit/EdSet08.readme"


def test_archive_paths_are_checked():
    assert aminet.check_path("https://aminet.net/util/arc/lha.run".replace(".run", ".lha")) == "util/arc/lha.lha"
    for bad in ("../etc/passwd", "package/game/edit/EdSet08", "nothing"):
        with pytest.raises(ValueError):
            aminet.check_path(bad)
    assert aminet.unpack_line("lha", "T:a.lha", "SYS:Games") == 'lha -q -m x "T:a.lha" "SYS:Games/"'
    assert aminet.tool_for("x.LZX") == "unlzx"


@pytest.mark.skipif(not shutil.which("jlha"), reason="no jlha to make archives on this PC")
@pytest.mark.parametrize("method", ["5", "6", "7"])
def test_lha_methods_unpack(tmp_path, method):
    body = (b"Amigent " * 3000) + bytes(range(256)) * 40
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "file.txt").write_bytes(body)
    done = subprocess.run(["jlha", f"ao{method}q", "t.lha", "dir/file.txt"], cwd=tmp_path,
                          capture_output=True)
    assert done.returncode == 0, done.stdout + done.stderr
    members = lzh.extract_all((tmp_path / "t.lha").read_bytes())
    assert members == {"dir/file.txt": body}


# ---- several steps in one call


def test_steps_run_in_order_and_stop_at_a_failure(monkeypatch):
    done = []
    monkeypatch.setattr(server, "amiga_click", lambda x, y, b="left", d=False: done.append(("click", x, y)) or f"clicked {x},{y}")
    monkeypatch.setattr(server, "amiga_key", lambda k: done.append(("key", k)) or f"pressed {k}")

    def broken(*a, **k):
        raise AmigaError("no such window")

    monkeypatch.setattr(server, "amiga_wait_window", broken)
    out = server.amiga_do([{"click": [1, 2]}, {"key": "return"}, {"wait_window": "X"}, {"key": "esc"}])
    assert done == [("click", 1, 2), ("key", "return")]
    assert "step 3 failed: no such window" in out[0] and "step 4" in out[0]


def test_steps_need_a_known_action():
    out = server.amiga_do([{"jump": 1}])
    assert "step 1 failed" in out[0]
    with pytest.raises(AmigaError):
        server.amiga_do([])


def test_wait_reports(monkeypatch):
    class C:
        def wait_for(self, kind, what, timeout, known=None):
            if what == "slow":
                raise AmigaError("timeout", 2)
            return [f"W\t0000beef\t1\t2\t30\t40\ta\t{what}"]

    monkeypatch.setattr(server, "ctl", C())
    monkeypatch.setattr(server, "_with_ctl", lambda fn: fn())
    assert server.amiga_wait_window("Req") == "window 'Req' 0000beef (1,2 30x40) active"
    assert server.amiga_wait_window("slow", timeout=3).startswith("timeout")
    assert server.amiga_wait_window("Req", gone=True) == "window 'Req' is gone"


def test_changed_box_covers_the_new_tiles(monkeypatch):
    class S:
        changes = [(1.0, 0, 0, 64, 64), (5.0, 128, 64, 64, 64), (6.0, 192, 128, 64, 32)]

    monkeypatch.setattr(server, "screen", S())
    assert server._changed_box(2.0) == (128, 64, 128, 96)
    assert server._changed_box(9.0) is None


def test_separate_changes_stay_separate(monkeypatch):
    class S:
        changes = [(5.0, 0, 0, 64, 64), (5.0, 64, 0, 64, 64), (5.0, 1856, 1024, 64, 56)]

    monkeypatch.setattr(server, "screen", S())
    assert server._changed_boxes(1.0) == [(0, 0, 128, 64), (1856, 1024, 64, 56)]


def test_link_report_names_the_mode(monkeypatch):
    monkeypatch.setattr(server, "_with_ctl", lambda fn: None)
    report = json.loads(server.amiga_link("best"))
    assert report["mode"] == "best"


def test_new_window_after_a_start_excludes_those_open_before(monkeypatch):
    seen = {}

    class C:
        def exec(self, line, cd=None, timeout=None):
            return None

        def wait_for(self, kind, what, timeout, known=None):
            seen["kind"], seen["known"] = kind, known
            return [f"W\t0000beef\t1\t2\t30\t40\ta\t{what}"]

    monkeypatch.setattr(server, "ctl", C())
    monkeypatch.setattr(server, "_with_ctl", lambda fn: fn())
    monkeypatch.setattr(server, "_clear_hold", lambda: "")
    monkeypatch.setattr(server, "_window_ids", lambda: [0x1000, 0x2000])
    server.amiga_start("SYS:Utilities/Clock")
    server.amiga_wait_window("Clock", new=True)
    assert seen["known"] == [0x1000, 0x2000] and seen["kind"] & 8
    server.amiga_wait_window("Clock")
    assert seen["known"] is None


def test_multi_assigns_are_spread_out():
    out = "LIBS           AmiKit:Libs\n             + AmiKit:Classes\n             + Work:MUI/Libs\n"
    assert install.parse_assign(out) == ["AmiKit:Libs", "AmiKit:Classes", "Work:MUI/Libs"]

    class C:
        def exec(self, line, cd=None, timeout=None):
            from amigent.client import ExecResult
            return ExecResult(0, out if "LIBS" in line else "C              AmiKit:C\n")

    assert install.expand(C(), ["C:", "LIBS:", "AmiKit:Classes", "SYS:Games/X"]) == \
        ["C:", "AmiKit:Libs", "AmiKit:Classes", "Work:MUI/Libs", "SYS:Games/X"]


def test_notes_reach_the_monitor_without_an_answer():
    fake = Fake5(files={"S:f": b"abc"})
    ch = channel_to(fake, mode="off")
    ch.note("start HiP morning.xm")
    ch.note("x" * 500)
    assert ch.read_file("S:f") == b"abc"  # the stream is still in step
    notes = [p for c, _, p in fake.seen if c == 37]
    assert notes == ["start HiP morning.xm", "x" * 160]


def test_no_notes_for_an_amigentd_that_does_not_know_them():
    fake = Fake5(files={"S:f": b"abc"}, notes=False)
    ch = channel_to(fake, mode="off")
    ch.note("hello")
    assert ch.read_file("S:f") == b"abc"
    assert not any(c == 37 for c, _, _ in fake.seen)


def test_note_lines_are_short_and_keep_secrets():
    from amigent.server import note_line
    assert note_line("amiga_start", {"command": "SYS:Utilities/HiP  x.xm"}) == "start SYS:Utilities/HiP x.xm"
    assert note_line("amiga_put", {"local_path": "a", "key": "s3cret"}) == "put a"
    long = note_line("amiga_exec", {"command": "Echo " + "y" * 200, "cd": "", "timeout": 30})
    assert len(long) < 130 and long.endswith("30")


def test_note_lines_leave_out_defaults():
    from amigent.server import note_line
    assert note_line("amiga_exec", {"command": "Echo hi", "timeout": 30}, {"timeout": 30}) == "exec Echo hi"
    assert note_line("amiga_exec", {"command": "Echo hi", "timeout": 90}, {"timeout": 30}) == "exec Echo hi 90"
    assert note_line("amiga_screen_text", {"x": 0, "y": 0, "w": 0, "h": 0},
                     {"x": 0, "y": 0, "w": 0, "h": 0}) == "screen_text"


def test_relative_drag_walks_the_way_in_small_steps(monkeypatch):
    from amigent import amigentd as ad
    monkeypatch.setattr(ad.time, "sleep", lambda s: None)
    calls = []
    a = ad.Amigentd.__new__(ad.Amigentd)
    a.move = lambda x, y: calls.append(("move", x, y))
    a.button = lambda b, down: calls.append(("button", down))
    a.move_relative = lambda dx, dy: calls.append(("rel", dx, dy))
    a.planar, a.width, a.height = False, 1920, 1080  # graphics card: one to one
    a.pointer = lambda: None  # an amigentd that can't tell
    a.drag(100, 50, 61, 70, relative=True)
    rel = [c for c in calls if c[0] == "rel"]
    assert calls[0] == ("move", 100, 50) and calls[1] == ("button", True) and calls[-1] == ("button", False)
    assert sum(c[1] for c in rel) == -39 and sum(c[2] for c in rel) == 20
    assert all(abs(c[1]) <= ad.DRAG_STEP and abs(c[2]) <= ad.DRAG_STEP for c in rel)
    assert not any(c[0] == "move" for c in calls[1:])


def test_mouse_counts_follow_the_screen_mode():
    from amigent import amigentd as ad
    a = ad.Amigentd.__new__(ad.Amigentd)
    for planar, w, h, want in ((False, 1920, 1080, (1, 1)), (True, 640, 256, (1, 2)),
                               (True, 320, 256, (2, 2)), (True, 640, 512, (1, 1)),
                               (True, 1280, 256, (0.5, 2))):
        a.planar, a.width, a.height = planar, w, h
        assert a.mickeys_per_pixel() == want


def test_relative_drag_corrects_for_acceleration(monkeypatch):
    """The Amiga speeds big moves up; the pointer position tells how much to take back."""
    from amigent import amigentd as ad
    monkeypatch.setattr(ad.time, "sleep", lambda s: None)
    a = ad.Amigentd.__new__(ad.Amigentd)
    a.planar, a.width, a.height = False, 1920, 1080
    at = [100, 100]

    def rel(dx, dy):  # acceleration: moves over 4 counts go one and a half times as far
        k = 1.5 if max(abs(dx), abs(dy)) > 4 else 1
        at[0] += round(dx * k)
        at[1] += round(dy * k)

    a.move = lambda x, y: at.__setitem__(slice(0, 2), [x, y])
    a.button = lambda b, down: None
    a.move_relative = rel
    a.pointer = lambda: tuple(at)
    a.drag(100, 100, 160, 220, relative=True)
    assert abs(at[0] - 160) <= 1 and abs(at[1] - 220) <= 1


def test_ui_tree_reads_the_pointer_from_the_screen_line():
    from amigent import amigentd as ad
    a = ad.Amigentd.__new__(ad.Amigentd)
    a._text = lambda cmd, a=0, b=0, arg=0: [["S", "085b63b0", "1920", "1080", "WB", "", "12", "-3"]]
    assert a.pointer() == (12, -3)
    assert a.ui_tree()[0]["mouse"] == (12, -3)
    a._text = lambda cmd, a=0, b=0, arg=0: [["S", "085b63b0", "1920", "1080", "WB"]]
    assert a.pointer() is None


def test_dos_errors_get_their_number_off_and_a_hint_on():
    from amigent.channel import dos_text
    assert dos_text("Objekt ist in Gebrauch [202]").startswith("Objekt ist in Gebrauch (another program")
    assert "Assign" in dos_text("Gerät nicht angemeldet [218]")
    assert dos_text("Objekt nicht gefunden [205]") == "Objekt nicht gefunden"
    assert dos_text("plain text") == "plain text"


def test_moves_off_the_screen_stop_at_its_edge():
    from amigent import amigentd as ad
    a = ad.Amigentd.__new__(ad.Amigentd)
    sent = []
    a._ok = lambda *args: sent.append(args)
    a.width, a.height, a.protocol = 640, 256, 5
    a.screens = lambda: [{"front": True, "width": 640, "height": 256}]
    a.move(-20, 300)
    assert sent[-1][2:] == (0, 255)


def test_the_edge_is_the_front_screens_not_the_last_pictures():
    """An interlaced screen came to the front after the last picture of a 640x256 one."""
    from amigent import amigentd as ad
    a = ad.Amigentd.__new__(ad.Amigentd)
    sent = []
    a._ok = lambda *args: sent.append(args)
    a.width, a.height, a.protocol = 640, 256, 5
    a.screens = lambda: [{"front": True, "width": 640, "height": 512},
                         {"front": False, "width": 640, "height": 256}]
    a.move(320, 400)
    assert sent[-1][2:] == (320, 400)
    a.move(320, 900)
    assert sent[-1][2:] == (320, 511)


def test_drag_moves_the_mouse_by_default_so_window_gadgets_follow(monkeypatch):
    """A drag bar or size gadget ignores a pointer jump; mouse movement moves it."""
    from amigent import amigentd as ad
    monkeypatch.setattr(ad.time, "sleep", lambda s: None)
    calls = []
    a = ad.Amigentd.__new__(ad.Amigentd)
    a._sock, a.protocol = object(), 5
    a.planar, a.width, a.height = False, 1920, 1080
    a.move = lambda x, y: calls.append(("move", x, y))
    a.button = lambda b, down: calls.append(("button", down))
    a.move_relative = lambda dx, dy: calls.append(("rel", dx, dy))
    a.pointer = lambda: None  # can't tell where it ended: a jump makes the end exact
    a.drag(1866, 576, 1600, 420)
    rel = [c for c in calls if c[0] == "rel"]
    assert calls[1] == ("button", True) and calls[-1] == ("button", False)
    assert sum(c[1] for c in rel) == -266 and sum(c[2] for c in rel) == -156
    assert calls[-2] == ("move", 1600, 420)


def test_drag_calibrates_against_mouse_acceleration(monkeypatch):
    """The pointer covers 0.85 of the steps across and 1.6 down (as measured on a
    PiStorm); a dry walk with the button up finds the distance to send."""
    from amigent import amigentd as ad
    monkeypatch.setattr(ad.time, "sleep", lambda s: None)
    calls, pos, held = [], [0.0, 0.0], []
    a = ad.Amigentd.__new__(ad.Amigentd)
    a._sock, a.protocol = object(), 5
    a.planar, a.width, a.height = False, 1920, 1080

    def move(x, y):
        pos[:] = [x, y]
        calls.append(("move", x, y))

    def rel(dx, dy):
        pos[0] += dx * 0.85
        pos[1] += dy * 1.6
        calls.append(("rel", dx, dy, bool(held)))

    def button(b, down):
        held[:] = [1] if down else []
        calls.append(("button", down))

    a.move, a.move_relative, a.button = move, rel, button
    a.pointer = lambda: (round(pos[0]), round(pos[1]))
    a.drag(1866, 576, 1600, 420)
    pressed = calls.index(("button", True))
    walked = [c for c in calls[pressed:] if c[0] == "rel"]
    assert walked and all(c[3] for c in walked)
    sent_x, sent_y = sum(c[1] for c in walked), sum(c[2] for c in walked)
    assert abs(sent_x * 0.85 + 266) <= 2 and abs(sent_y * 1.6 + 156) <= 2
    assert [c for c in calls[pressed:] if c[0] == "move"] == []  # landed: no jump


def test_drag_jumps_on_an_old_amigentd(monkeypatch):
    from amigent import amigentd as ad
    monkeypatch.setattr(ad.time, "sleep", lambda s: None)
    calls = []
    a = ad.Amigentd.__new__(ad.Amigentd)
    a._sock, a.protocol = object(), 1
    a.move = lambda x, y: calls.append(("move", x, y))
    a.button = lambda b, down: calls.append(("button", down))
    a.drag(10, 10, 50, 60)
    assert calls == [("move", 10, 10), ("button", True), ("move", 50, 60), ("button", False)]
