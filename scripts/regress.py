"""Regression test of a real Amiga (or an emulator) through the MCP tools, before a release.

Usage: regress.py HOST [--port 6801] [--rexx AMIGENT] [--report FILE] [--only NAMES]
                       [--pointer RAM:pointer-test] [--kprintf PATH] [--aminet PATH]

Runs every tool an AI agent uses against the Amiga at HOST and prints PASS,
FAIL or SKIP per check, with the time it took. Leaves nothing behind but
RAM:amigent-regress while it runs; that is deleted at the end. Opens one
Shell window of its own and closes it again; keep the mouse off the Amiga
meanwhile.

--port      amigentd's port; another one tests a second amigentd started next
            to the installed one (amigentd PORT 6811 KEY S:amigent.key QUIET)
--rexx      its ARexx port, when not AMIGENT or AMIGENT.<port>; STATUS tells which
--report    also write the results as Markdown to FILE
--only      comma separated check names, e.g. files,typing
--pointer   also run scripts/pointer_test.py with this test program
            (amiga/pointer-test.c, built on the Amiga)
--kprintf   a program that writes through RawPutChar (amiga/kprintf-test.c,
            built for the Amiga), here or on the Amiga, for the debug check
--aminet    a small Aminet archive to install into RAM:amigent-regress

Exit code 0 when nothing failed. What it learned over earlier releases:
- long commands go through amiga_job, never amiga_exec; nothing lists a
  big volume recursively;
- a short right click on a Directory Opus desktop leaves a popup that holds
  every new window back: the hold check uses a held right button instead;
- typing ahead of a busy Shell is fine, it waits in the type-ahead;
- after a crash amiga_status tells the restart; run it again after a power cycle;
- from 1.3.0: text first, pictures when the text says it is not enough; packing
  pays only when the Amiga packs faster than its line carries, so it is
  forced both ways here and must give the same bytes.
"""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIR = "RAM:amigent-regress"
TITLE = "amigent-regress"


def setup(args):
    os.environ["AMIGENT_HOST"] = args.host
    os.environ["AMIGENT_SCREEN_PORT"] = str(args.port)
    sys.path.insert(0, str(ROOT / "src"))
    import amigent.server as server  # noqa: E402  reads the variables above
    return server


class Run:
    def __init__(self, server, args):
        self.s, self.args, self.results = server, args, []

    def check(self, name, fn):
        if self.args.only and name not in self.args.only:
            return
        start = time.monotonic()
        try:
            detail = fn() or ""
            verdict = "SKIP" if str(detail).startswith("skip") else "PASS"
        except Exception as e:  # a failed check must not stop the others
            verdict, detail = "FAIL", f"{type(e).__name__}: {e}"
            if os.environ.get("REGRESS_TRACE"):
                traceback.print_exc()
        took = time.monotonic() - start
        self.results.append((verdict, name, took, str(detail)))
        print(f"{verdict:4} {name:14} {took:6.1f} s  {str(detail)[:150]}", flush=True)

    # ---- the checks ------------------------------------------------------

    def status(self):
        import json
        info = json.loads(self.s.amiga_status())
        sysinfo = info["sysinfo"]
        assert int(sysinfo["chip_total"]) > 0 and sysinfo.get("kickstart"), sysinfo
        notes = "; ".join(info.get("notes", []))
        return (f"Kickstart {sysinfo['kickstart']}, {sysinfo['cpu']}, up "
                f"{info.get('running_for', '?')}{', ' + notes if notes else ''}")

    def version(self):
        out = self.s.amiga_exec("Version C:amigentd")
        assert out.startswith("rc=0"), out
        return f"installed: {out.splitlines()[-1]}"

    def exec(self):
        out = self.s.amiga_exec("Echo \"Grüße aus Amigent\"")
        assert out == "rc=0\nGrüße aus Amigent\n", repr(out)
        bad = self.s.amiga_exec("Assign NOSUCHASSIGN: EXISTS")
        assert not bad.startswith("rc=0"), bad
        return "output, umlauts and return codes"

    def missing_volume(self):
        start = time.monotonic()
        out = self.s.amiga_exec("Dir NOSUCHVOLUME:", timeout=20)
        took = time.monotonic() - start
        assert not out.startswith("rc=0") and took < 10, f"{took:.1f} s: {out}"
        return f"fails in {took:.1f} s instead of a requester"

    def job(self):
        out = self.s.amiga_job("Echo job-done", wait=60)
        assert "job-done" in out, out
        started = time.monotonic()
        self.s.amiga_job("Wait 30", wait=1)
        said = self.s.amiga_break("Wait")
        assert "sent Ctrl-C" in said, said
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if "done" in self.s.amiga_exec(f"Type RAM:amigent-job.done"):
                return f"job and break, Wait ended after {time.monotonic() - started:.1f} s"
            time.sleep(1)
        raise AssertionError("Wait did not end after Ctrl-C")

    def files(self):
        self.s.amiga_mkdir(DIR)
        text = "Zeile 1: äöü ÄÖÜ ß\nZeile 2: <>&\"'\n"
        self.s.amiga_write_text(f"{DIR}/Umlaut äöü.txt", text)
        assert self.s.amiga_read_text(f"{DIR}/Umlaut äöü.txt") == text
        data = random.Random(7).randbytes(150_000)
        with tempfile.TemporaryDirectory() as tmp:
            src, back = Path(tmp) / "blob.bin", Path(tmp) / "back.bin"
            src.write_bytes(data)
            start = time.monotonic()
            self.s.amiga_put(str(src), f"{DIR}/blob.bin")
            put = time.monotonic() - start
            self.s.amiga_move(f"{DIR}/blob.bin", f"{DIR}/moved.bin")
            self.s.amiga_get(f"{DIR}/moved.bin", str(back))
            assert back.read_bytes() == data, "file came back different"
        listing = self.s.amiga_list(DIR)
        assert "moved.bin" in listing and "Umlaut äöü.txt" in listing, listing
        self.s.amiga_delete(f"{DIR}/moved.bin")
        assert "moved.bin" not in self.s.amiga_list(DIR)
        return f"150 KB there in {put:.1f} s ({150 / max(put, 0.01):.0f} KB/s) and back the same"

    def screen(self):
        shot = self.s.amiga_screenshot()
        assert len(shot) == 2 and shot[1].data[:4] == b"\x89PNG", shot[0]
        colour = self.s.amiga_screenshot_color(0, 0, 64, 32)
        assert colour[1].data[:4] == b"\x89PNG"
        screens = self.s.amiga_screens()
        assert " front" in screens, screens
        quiet = self.s.amiga_wait_change(0, 0, 64, 16, timeout=5)
        assert "slow" not in quiet, quiet
        return f"{shot[0].split(';')[0]}; {len(screens.splitlines())} screen(s); wait_change: {quiet}"

    def open_shell(self):
        # A Shell opens on Workbench; a screen left in front would hide it.
        if "workbench" in self.s.amiga_screens() and " front workbench" not in self.s.amiga_screens():
            self.s.amiga_screen_front("Workbench")
            time.sleep(1)
        said = self.s.amiga_start(f"NewShell WINDOW CON:40/60/560/160/{TITLE}/CLOSE")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            tree = self.s.screen.ui_tree()
            win = [w for w in tree if w["kind"] == "window" and w["title"] == TITLE]
            if win:
                w = win[0]
                self.s.amiga_click(w["x"] + w["w"] // 2, w["y"] + w["h"] // 2)
                time.sleep(0.5)
                return f"{said}; window at ({w['x']},{w['y']})"
            time.sleep(1)
        raise AssertionError("the Shell window did not open")

    def _shell_active(self):
        """Our Shell gets the keys again: something else may have opened in between."""
        for _ in range(3):
            tree = self.s.screen.ui_tree()
            ours = [w for w in tree if w["kind"] == "window" and w["title"] == TITLE]
            if not ours:
                self.open_shell()
                continue
            if ours[0]["active"]:
                return
            w = ours[0]
            self.s.amiga_click(w["x"] + w["w"] // 2, w["y"] + w["h"] // 2)
            time.sleep(0.5)
        raise AssertionError("cannot get our Shell window active")

    def typing(self):
        self._shell_active()
        self.s.amiga_type(f"Echo >{DIR}/typed.txt \"äöüß ÄÖÜ @{{}}[]\"\n", settle=0)
        time.sleep(2)
        got = self.s.amiga_read_text(f"{DIR}/typed.txt").strip()
        assert got == "äöüß ÄÖÜ @{}[]", repr(got)
        start = time.monotonic()
        said = self.s.amiga_type(f"Echo >{DIR}/l1.txt eins\nWait 2\nEcho >{DIR}/l2.txt zwei\n")
        took = time.monotonic() - start
        time.sleep(4)
        assert self.s.amiga_read_text(f"{DIR}/l2.txt").strip() == "zwei", "the last line got lost"
        self.s.amiga_key("ctrl+c")
        return f"keymap characters; {said} in {took:.1f} s"

    def ui(self):
        self._shell_active()
        tree = self.s.amiga_ui_tree(pictures=False)[0]
        assert TITLE in tree and "active" in tree, tree[:300]
        menus = self.s.amiga_menus()
        return f"{len(tree.splitlines())} lines; menus: {menus.splitlines()[0][:40] if menus else '-'}"

    def hold(self):
        self._shell_active()
        if self.s.screen.held() == "" and self.s.screen.protocol < 4:
            return "skip: amigentd too old"
        tree = self.s.screen.ui_tree()
        top = next(w for w in tree if w["kind"] == "window" and w["title"] == TITLE)
        self.s.amiga_mouse_move(top["x"] + 40, 2)  # over the screen's title bar
        self.s.amiga_button("right", True)
        try:
            time.sleep(0.8)
            state = self.s.screen.held()
        finally:
            self.s.amiga_button("right", False)
        time.sleep(0.5)
        after = self.s.screen.held()
        if state == "" and after == "":
            return "skip: amigentd says nothing (before 1.3.0?)"
        assert state in ("menu", "held") and after == "", (state, after)
        return f"right button held: {state}; let go: nothing"

    def _rexx_port(self):
        """The ARexx port of the amigentd under test: STATUS tells its TCP port."""
        ports = self.s.amiga_rexx_ports().split()
        names = [self.args.rexx] if self.args.rexx else [f"AMIGENT.{self.args.port}", "AMIGENT"]
        for name in names:
            if name in ports and f"port={self.args.port}" in self.s.amiga_arexx("STATUS", port=name):
                return name
        return ""

    def arexx(self):
        port = self._rexx_port()
        if not port:
            return "skip: no ARexx port of this amigentd (RexxMast not running?)"
        out = self.s.amiga_arexx("VERSION", port=port)
        assert out.startswith("rc=0") and "amigentd" in out, out
        status = self.s.amiga_arexx("STATUS", port=port)
        assert "protocol=" in status, status
        help_ = self.s.amiga_arexx("HELP", port=port)
        if "ACTIVATEWINDOW" not in help_:
            return f"{out.splitlines()[-1]}; no typing commands (before 1.3.0)"
        busy = self.s.amiga_arexx("BUSY", port=port) if "BUSY" in help_ else "rc=0\n"
        assert busy.startswith("rc=0") and busy.strip().endswith(("rc=0", "")), busy
        typed = self.s.amiga_arexx(f'ACTIVATEWINDOW "{TITLE}"\n'
                                   f'TYPE "Echo >{DIR}/rexx.txt Grüße<return>"', port=port)
        assert typed.startswith("rc=0"), typed
        time.sleep(2)
        got = self.s.amiga_read_text(f"{DIR}/rexx.txt").strip()
        assert got == "Grüße", repr(got)
        missing = self.s.amiga_arexx('ACTIVATEWINDOW "no such window here"', port=port)
        assert missing.startswith("rc=5"), missing
        return f"{out.splitlines()[-1]}; ACTIVATEWINDOW, TYPE, BUSY"

    def pause(self):
        try:
            said = self.s.amiga_pause_screen(5)
        except Exception as e:
            return f"skip: {e}"
        try:
            note = self.s.amiga_screenshot()[0]
            assert "paused" in note, note
        finally:
            self.s.amiga_pause_screen(0)
        assert len(self.s.amiga_screenshot(0, 0, 32, 32)) == 2
        return said

    def takeover(self):
        from amigent.amigentd import Amigentd
        other = Amigentd(self.args.host, self.args.port)
        other.connect()
        other.ping()
        other.close()
        start = time.monotonic()
        for _ in range(20):
            probe = Amigentd(self.args.host, self.args.port)
            probe.connect()
            probe.close()
        self.s.amiga_screenshot(0, 0, 32, 32)  # the server's own client takes over again
        assert self.s.amiga_exec("Echo still").endswith("still\n")
        return f"second client, then 20 connections in {time.monotonic() - start:.1f} s; all fine"

    def close_shell(self):
        for _ in range(4):  # one EndShell per Shell of ours, should there be more
            tree = self.s.screen.ui_tree()
            if not any(w["kind"] == "window" and w["title"] == TITLE for w in tree):
                break
            self._shell_active()
            self.s.amiga_type("EndShell\n", settle=0)
            time.sleep(2)
        self.s.amiga_exec(f"Delete {DIR} ALL QUIET FORCE")
        deadline = time.monotonic() + 15  # a 68000 takes a while to close a window
        while time.monotonic() < deadline:
            tree = self.s.screen.ui_tree()
            if not any(w["kind"] == "window" and w["title"] == TITLE for w in tree):
                return "Shell closed, RAM:amigent-regress deleted"
            time.sleep(1)
        raise AssertionError("the Shell window is still open: " +
                             repr([w["title"] for w in tree if w["kind"] == "window"]))

    # ---- 1.3.0 part two -------------------------------------------------

    def _new(self):
        info = self.s.ctl.sysinfo()
        return "text" in info

    def screen_text(self):
        if not self._new():
            return "skip: amigentd before protocol 5"
        self._shell_active()
        word = f"regress{random.randint(1000, 9999)}"
        self.s.amiga_type(f"Echo {word}-out\n", settle=0)
        waited = self.s.amiga_wait_text(f"{word}-out", timeout=15)
        assert waited.startswith("text"), waited
        text = self.s.amiga_screen_text(window=TITLE)
        assert f"{word}-out" in text, text[-400:]
        found = self.s.amiga_screen_text(find=word)
        assert TITLE in found, found
        clicked = self.s.amiga_click_text(f"{word}-out", window=TITLE)
        return f"{len(text.splitlines())} lines from our Shell; {clicked}"

    def wait(self):
        if not self._new():
            return "skip: amigentd before protocol 5"
        start = time.monotonic()
        got = self.s.amiga_wait_window(TITLE, timeout=5)
        assert got.startswith("window") and TITLE in got, got
        quick = time.monotonic() - start
        start = time.monotonic()
        none = self.s.amiga_wait_window("no such window title here", timeout=3)
        took = time.monotonic() - start
        assert none.startswith("timeout") and 2.5 < took < 12, (none, took)
        gone = self.s.amiga_wait_text("no such text anywhere 4711", timeout=2, gone=True)
        assert "gone" in gone, gone
        return f"window found in {quick:.1f} s, timeout after {took:.1f} s"

    def steps(self):
        if not self._new():
            return "skip: amigentd before protocol 5"
        self._shell_active()
        out = self.s.amiga_do([{"type": f"Echo steps-done >{DIR}/steps.txt\n", "settle": 0},
                               {"wait": 1},
                               {"exec": f"Type {DIR}/steps.txt"}], look="text")
        assert "step" not in out[0] or "failed" not in out[0], out[0]
        assert "steps-done" in out[0], out[0]
        changed = self.s.amiga_screenshot(changed=True)
        assert changed[0].startswith(("changed", "no change")), changed[0]
        return f"3 steps, look=text {len(out[1].splitlines())} lines; {changed[0][:60]}"

    def packing(self):
        from amigent import pack
        if not self._new():
            return "skip: amigentd before protocol 5"
        policy = pack.policy_for(self.s.HOST)
        before = policy.chosen
        data = random.Random(3).randbytes(20_000) + b"Workbench text that packs " * 2000
        results = {}
        try:
            for mode in ("off", "fast", "best"):
                policy.mode = mode
                start = time.monotonic()
                self.s.ctl.write_file(f"{DIR}/pack-{mode}.bin", data)
                back = self.s.ctl.read_file(f"{DIR}/pack-{mode}.bin")
                out = self.s.amiga_exec(f"List {DIR}")
                results[mode] = time.monotonic() - start
                assert back == data, f"{mode}: the file came back different"
                assert f"pack-{mode}.bin" in out, out
        finally:
            policy.mode = before or "auto"
        auto = policy.describe()
        return (", ".join(f"{m} {t:.1f} s" for m, t in results.items()) +
                f"; auto picks {auto['from_amiga']} (Amiga packs {auto['amiga_packs_kbs']} KB/s, "
                f"line {auto['line_kbs']} KB/s)")

    def tree(self):
        if not self._new():
            return "skip: amigentd before protocol 5"
        self.s.amiga_mkdir(f"{DIR}/tree/sub")
        self.s.amiga_write_text(f"{DIR}/tree/sub/a.txt", "abc")
        entries = self.s.ctl.tree(f"{DIR}/tree", crc=True)
        names = {e["name"]: e for e in entries}
        import zlib
        assert names["sub/a.txt"]["crc"] == zlib.crc32(b"abc"), names
        assert names["sub"]["type"] == "DIR"
        return f"{len(entries)} entries with CRC in one answer"

    def install_undo(self):
        if not self._new():
            return "skip: amigentd before protocol 5"
        base = f"{DIR}/inst"
        self.s.amiga_mkdir(base)
        self.s.amiga_write_text(f"{base}/keep.txt", "same")
        self.s.amiga_write_text(f"{base}/change.txt", "old")
        self.s.amiga_write_text(f"{base}/gone.txt", "bye")
        name = f"regress{random.randint(100, 999)}"
        home = self.s.AMIGA_DIR
        self.s.AMIGA_DIR = f"{DIR}/home"
        try:
            self.s.amiga_install_begin(name, folders=base, system=False)
            self.s.amiga_write_text(f"{base}/new.txt", "new")
            self.s.amiga_write_text(f"{base}/change.txt", "changed!")
            self.s.amiga_delete(f"{base}/gone.txt")
            ended = self.s.amiga_install_end(name)
            assert "1 added, 1 changed, 1 removed" in ended, ended
            undone = self.s.amiga_install_undo(name)
            assert "taken back" in undone and "except" not in undone, undone
        finally:
            self.s.AMIGA_DIR = home
        assert self.s.amiga_read_text(f"{base}/change.txt") == "old"
        assert self.s.amiga_read_text(f"{base}/gone.txt") == "bye"
        assert "new.txt" not in self.s.amiga_list(base)
        return f"added, changed and removed, then all taken back ({name})"

    def debug(self):
        if not self._new():
            return "skip: amigentd before protocol 5"
        self.s.amiga_debug_log()  # what is old
        if not self.args.kprintf:
            said = self.s.amiga_debug_log()
            return f"skip: no --kprintf program; debug log answers ({said[:40]!r})"
        report = self.s.amiga_run(self.args.kprintf)
        assert "rc=0" in report and "hello from kprintf" in report, report
        return "amiga_run: rc and kprintf output reported"

    def clipboard(self):
        old = self.s.amiga_clipboard()
        try:
            self.s.amiga_clipboard("Grüße aus der Regression")
            assert self.s.amiga_clipboard() == "Grüße aus der Regression"
        finally:
            if old and not old.startswith(("the clipboard is empty", "the clipboard holds")):
                self.s.amiga_clipboard(old)
        return "set and read back, then restored"

    def notes(self):
        topic = "amigent-regress"
        self.s.amiga_note(topic, "a note for the regression")
        assert "a note for the regression" in self.s.amiga_notes(topic)
        assert "amigent-regress" in self.s.amiga_notes(search="regression")
        self.s.amiga_note(topic, remove="*")
        assert "no notes" in self.s.amiga_notes(topic)
        return "written, found, removed"

    def aminet(self):
        found = self.s.amiga_aminet_search("lha", limit=5)
        assert "/" in found, found
        if not self.args.aminet:
            return f"search: {found.splitlines()[0][:70]}; skip install (no --aminet)"
        said = self.s.amiga_aminet_install(self.args.aminet, f"{DIR}/aminet")
        assert "into" in said, said
        return said[:140]

    def pointer(self):
        if not self.args.pointer:
            return "skip: no --pointer test program given"
        done = subprocess.run([sys.executable, str(ROOT / "scripts" / "pointer_test.py"),
                               self.args.host, str(self.args.port), self.args.pointer],
                              capture_output=True, text=True, timeout=600)
        last = (done.stdout.strip().splitlines() or ["no output"])[-1]
        assert done.returncode == 0, done.stdout[-500:] + done.stderr[-500:]
        return last


ORDER = ["status", "version", "exec", "missing_volume", "job", "files", "screen", "open_shell",
         "typing", "ui", "hold", "arexx", "screen_text", "wait", "steps", "packing", "tree",
         "install_undo", "debug", "clipboard", "notes", "aminet", "pause", "takeover", "pointer",
         "close_shell"]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("host")
    p.add_argument("--port", type=int, default=6801)
    p.add_argument("--rexx", default="")
    p.add_argument("--report")
    p.add_argument("--only", type=lambda v: set(v.split(",")))
    p.add_argument("--pointer", default="")
    p.add_argument("--kprintf", default="")
    p.add_argument("--aminet", default="")
    args = p.parse_args()
    run = Run(setup(args), args)
    print(f"Amigent regression against {args.host}:{args.port}, {time.strftime('%Y-%m-%d %H:%M')}")
    for name in ORDER:
        run.check(name, getattr(run, name))
    failed = [r for r in run.results if r[0] == "FAIL"]
    summary = (f"{sum(r[0] == 'PASS' for r in run.results)} passed, {len(failed)} failed, "
               f"{sum(r[0] == 'SKIP' for r in run.results)} skipped")
    print(summary)
    if args.report:
        lines = [f"# Regression {args.host}:{args.port}, {time.strftime('%Y-%m-%d %H:%M')}", "",
                 summary, "", "| result | check | s | detail |", "|---|---|---|---|"]
        lines += [f"| {v} | {n} | {t:.1f} | {d.replace('|', '/')} |" for v, n, t, d in run.results]
        Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
