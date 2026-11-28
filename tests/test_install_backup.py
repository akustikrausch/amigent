"""Backups before an installation (#39): estimate, record of the system folders,
copies of what is touched, the backup as a job with a marker, honest undo."""
import json
import re

import pytest

from amigent import install, server
from amigent.client import ExecResult
from amigent.errors import AmigaError

join = install.join


def entry(size=10, date="2026-11-01 10:00:00", crc=1):
    return {"type": "FILE", "size": size, "date": date, "crc": crc}


class SimAmiga:
    """An Amiga with a file system in dicts, which runs the AmigaDOS scripts
    Amigent writes (MakeDir, Copy, Delete, Echo, If WARN/Skip/Lab, Execute)
    and the background jobs of amiga_job."""

    def __init__(self, files, dirs=(), free=10**9):
        self.files = dict(files)  # full path -> entry
        self.dirs = set(dirs) | {"RAM:", "C:", "LIBS:", "S:", "SYS:"}
        self.data = {}  # contents of files Amigent wrote or a script echoed
        self.assigns = {"SYS": "SYS:      System:"}
        self.free = free
        self.hold = False  # jobs stay running until run_held()
        self.held = []
        self.copies_allowed = None  # break the backup after this many copies
        self.copies = 0
        self.calls = []

    # the channel's calls

    def sysinfo(self):
        return {}

    def tree(self, path, crc=False):
        prefix = join(path, "")
        if path not in self.dirs:
            raise AmigaError("object not found", 205)
        out = [{"name": d[len(prefix):], "type": "DIR", "size": 0, "date": "", "protection": ""}
               for d in sorted(self.dirs) if d.startswith(prefix) and d != path]
        for f, e in sorted(self.files.items()):
            if f.startswith(prefix):
                item = {"name": f[len(prefix):], **e, "protection": "----rwed"}
                if not crc:
                    item.pop("crc", None)
                out.append(item)
        return out

    def write_file(self, path, data, append=False):
        self.data[path] = (self.data.get(path, b"") if append else b"") + data
        if not path.startswith("RAM:"):
            self.files[path] = entry(len(data), "now", hash(data) & 0xFFFF)

    def read_file(self, path):
        if path not in self.data:
            raise AmigaError("object not found", 205)
        return self.data[path]

    def stat(self, path):
        if path in self.data or path in self.files:
            return {"type": "file", "size": len(self.data.get(path, b""))}
        if path in self.dirs:
            return {"type": "dir"}
        raise AmigaError("object not found", 205)

    def delete(self, path):
        if path not in self.data and path not in self.files:
            raise AmigaError("object not found", 205)
        self.data.pop(path, None)
        self.files.pop(path, None)

    def list_dir(self, path):
        names = {p[len(path):] for p in self.data if p.startswith(path) and "/" not in p[len(path):]}
        return [{"name": n, "type": "FILE"} for n in sorted(names)]

    def volumes(self):
        return [{"name": "System:", "used": 1, "free": self.free, "capacity": 2, "blocksize": 512}]

    def exec(self, line, cd=None, timeout=None):
        self.calls.append(line)
        m = re.fullmatch(r"Assign (\S+): EXISTS", line)
        if m:
            said = self.assigns.get(m.group(1))
            return ExecResult(0, said) if said else ExecResult(5, "")
        m = re.fullmatch(r"Run >\S+ Execute (\S+)", line)
        if m:
            if self.hold:
                self.held.append(m.group(1))
            else:
                self.run(m.group(1))
            return ExecResult(0, "[CLI 7]")
        m = re.fullmatch(r"Execute (\S+)", line)
        if m:
            rc, out = self.run(m.group(1))
            return ExecResult(rc, out)
        return ExecResult(0, "")

    def run_held(self):
        for script in self.held:
            self.run(script)
        self.held = []

    # AmigaDOS, as far as Amigent's scripts need it

    def run(self, script):
        lines = self.data[script].decode("iso-8859-1").splitlines()
        rc, out, i = 0, [], 0
        while i < len(lines):
            line = lines[i].strip()
            i += 1
            # AmigaDOS takes a redirection right after the command word
            redirect = re.match(r"(\S+) >>(\S+)(?: (.*))?$", line)
            if redirect:
                m = redirect.group(1) == "Execute" and re.fullmatch(r"\S+", redirect.group(3) or "")
                rc, said = self.run(redirect.group(3)) if m else (0, "")
                self.write_file(redirect.group(2), said.encode("iso-8859-1"), append=True)
                continue
            words = re.findall(r'"[^"]*"|\S+', line)
            if not words:
                continue
            cmd, args = words[0], [w.strip('"') for w in words[1:]]
            if cmd == "Quit":  # ends every script up to the CLI, like AmigaDOS
                raise AssertionError("Quit in a job script ends the job's own script too")
            if cmd == "Skip":
                i = lines.index(f"Lab {args[0]}", i) + 1
                continue
            if cmd == "If" and args == ["WARN"]:
                if rc >= 5:
                    i = lines.index("Lab failed", i) + 1
                else:
                    i = lines.index("EndIf", i) + 1
                continue
            if cmd == "MakeDir":
                self.dirs.add(args[-1])
            elif cmd == "Delete":
                for p in args[1:]:
                    if p != "QUIET":
                        self.files.pop(p, None)
                        self.data.pop(p, None)
                        self.dirs.discard(p)
            elif cmd == "Echo":
                if words[1].startswith(">"):
                    target = words[1][1:].strip('"')
                    text = words[2].strip('"').replace("$RC", str(rc))
                    self.write_file(target, (text + "\n").encode("iso-8859-1"))
                else:
                    out.append(args[0])
            elif cmd == "Copy":
                rc = self.copy(args[0], args[1], "ALL" in args)
                out.append(f"{args[0]} copied" if not rc else f"{args[0]}: error")
        return rc, "\n".join(out)

    def copy(self, src, dst, everything):
        if self.copies_allowed is not None:
            if self.copies >= self.copies_allowed:
                return 10  # broken by Ctrl-C
            self.copies += 1
        parent = dst.rsplit("/", 1)[0] if "/" in dst else dst.split(":")[0] + ":"
        if src in self.dirs:
            prefix, base = join(src, ""), join(dst, "")
            self.dirs.add(dst)
            for d in [d for d in self.dirs if d.startswith(prefix) and d != src]:
                self.dirs.add(base + d[len(prefix):])
            for f, e in list(self.files.items()):
                if f.startswith(prefix):
                    self.files[base + f[len(prefix):]] = dict(e)
            return 0
        if src not in self.files or parent not in self.dirs:
            return 20
        self.files[dst] = dict(self.files[src])
        return 0


@pytest.fixture
def sim(monkeypatch):
    fake = SimAmiga({
        "C:Dir": entry(), "C:List": entry(),
        "LIBS:old.library": entry(500),
        "S:User-Startup": entry(40), "S:Startup-Sequence": entry(60),
        "SYS:Games/Old/game": entry(70),
    }, dirs={"SYS:Games", "SYS:Games/Old"})
    monkeypatch.setattr(server._session(), "ctl", fake)
    monkeypatch.setattr(server, "_with_ctl", lambda fn: fn())
    monkeypatch.setattr(server, "_home", lambda: "SYS:amigent")
    monkeypatch.setattr(install, "SYSTEM", ["C:", "LIBS:", "S:"])
    monkeypatch.setattr(server.files, "makedirs", lambda ctl, path: ctl.dirs.add(path))
    return fake


def test_default_copies_named_folders_and_the_startup_files_only(sim):
    said = server.amiga_install_begin("demo", folders="SYS:Games")
    assert "ready" in said and "to copy:" in said
    record = install.load(server._amiga(), "demo")
    assert record.copied == ["SYS:Games", "S:Startup-Sequence", "S:User-Startup"]
    assert record.backup == "complete" and record.job
    assert "SYS:amigent/undo/demo/SYS_Games/Old/game" in sim.files
    assert "SYS:amigent/undo/demo/S/User-Startup" in sim.files
    assert not any(f.startswith("SYS:amigent/undo/demo/C/") for f in sim.files)
    assert sim.data[record.marker].decode().strip() == record.started
    assert record.nocrc == ["C:", "LIBS:", "S:"]
    assert "crc" not in record.before["C:"]["Dir"] and "crc" in record.before["SYS:Games"]["Old/game"]
    # the job is an ordinary one: amiga_jobs names it
    assert "Amigent backup for installation demo" in server.amiga_jobs()


def test_undo_restores_what_it_has_and_names_what_it_has_not(sim):
    server.amiga_install_begin("demo", folders="SYS:Games")
    sim.files["S:User-Startup"] = entry(99, "later")  # copied before: can be restored
    sim.files["LIBS:old.library"] = entry(600, "later")  # recorded only: can't
    sim.files["LIBS:new.library"] = entry(5)
    del sim.files["C:List"]  # recorded only: can't
    sim.files["SYS:Games/Old/game"] = entry(71, "later")
    said = server.amiga_install_end("demo")
    assert "1 added, 3 changed, 1 removed" in said
    assert "undo can't restore them: LIBS:old.library, C:List" in said
    said = server.amiga_install_undo("demo")
    assert "as far as the backup goes" in said
    assert "LIBS:old.library" in said and "C:List" in said
    assert sim.files["S:User-Startup"]["size"] == 40
    assert sim.files["SYS:Games/Old/game"]["size"] == 70
    assert "LIBS:new.library" not in sim.files
    record = install.load(server._amiga(), "demo")
    assert record.undone and record.unrestorable == ["LIBS:old.library", "C:List"]


def test_touch_copies_named_system_files_so_undo_restores_them(sim):
    server.amiga_install_begin("demo", touch="LIBS:old.library")
    assert "SYS:amigent/undo/demo/LIBS/old.library" in sim.files
    sim.files["LIBS:old.library"] = entry(600, "later")
    said = server.amiga_install_end("demo")
    assert "can't restore" not in said
    assert "taken back: the recorded folders are as before" in server.amiga_install_undo("demo")
    assert sim.files["LIBS:old.library"]["size"] == 500
    with pytest.raises(AmigaError, match="not in any recorded folder"):
        server.amiga_install_begin("other", touch="Work:Elsewhere/x")


def test_files_put_while_open_are_copied_aside_first(sim, tmp_path, monkeypatch):
    server.amiga_install_begin("demo")
    local = tmp_path / "old.library"
    local.write_bytes(b"new version")
    # files.put writes through the fake ctl
    monkeypatch.setattr(server.files, "put",
                        lambda ctl, src, target: ctl.write_file(target, src.read_bytes()) or "sent")
    said = server.amiga_put(str(local), "LIBS:old.library")
    assert "copied 1 file(s) aside for the installation 'demo'" in said
    assert sim.files["SYS:amigent/undo/demo/LIBS/old.library"]["size"] == 500
    server.amiga_install_end("demo")
    assert "as before" in server.amiga_install_undo("demo")
    assert sim.files["LIBS:old.library"]["size"] == 500


def test_a_large_backup_asks_first_and_records_nothing(sim, monkeypatch):
    monkeypatch.setattr(install, "LIMIT_FILES", 1)
    said = server.amiga_install_begin("demo", full=True)
    assert "would be large" in said and "confirm=True" in said and "files" in said
    with pytest.raises(AmigaError):
        install.load(server._amiga(), "demo")
    assert not any(c.startswith("Run ") for c in sim.calls)
    said = server.amiga_install_begin("demo", full=True, estimate_only=True)
    assert said.startswith("estimate only") and "System: has" in said
    said = server.amiga_install_begin("demo", full=True, confirm=True)
    assert "ready" in said
    assert "SYS:amigent/undo/demo/C/List" in sim.files


def test_not_enough_room_stops_before_copying(sim):
    sim.free = 1000
    with pytest.raises(AmigaError, match="not enough room on System:"):
        server.amiga_install_begin("demo")
    assert not any(c.startswith("Run ") for c in sim.calls)


def test_a_broken_backup_is_never_taken_for_complete(sim):
    sim.copies_allowed = 1  # amiga_break after the first copy
    with pytest.raises(AmigaError, match="did not complete"):
        server.amiga_install_begin("demo", folders="SYS:Games")
    record = install.load(server._amiga(), "demo")
    assert record.backup == "failed" and record.marker not in sim.data
    with pytest.raises(AmigaError, match="did not complete"):
        server.amiga_install_end("demo")
    sim.copies_allowed = None
    said = server.amiga_install_begin("demo", folders="SYS:Games")
    assert "ready" in said


def test_a_backup_that_outlasts_the_wait_is_followed_up(sim):
    sim.hold = True
    said = server.amiga_install_begin("demo", folders="SYS:Games", wait=0)
    job = install.load(server._amiga(), "demo").job
    assert f"runs as job {job}" in said and f"amiga_break(job={job})" in said
    with pytest.raises(AmigaError, match="still runs"):
        server.amiga_install_end("demo")
    assert "still runs as job" in server.amiga_install_begin("demo")
    sim.run_held()
    assert server.amiga_install_begin("demo").startswith("ready")
    assert install.load(server._amiga(), "demo").backup == "complete"


def test_a_backup_whose_job_is_gone_counts_as_failed(sim):
    sim.hold = True
    server.amiga_install_begin("demo", folders="SYS:Games", wait=0)
    for name in [p for p in sim.data if p.startswith("RAM:amigent-job-")]:
        del sim.data[name]  # the Amiga restarted: RAM: is empty
    with pytest.raises(AmigaError, match="did not complete"):
        server.amiga_install_end("demo")


def test_an_old_marker_does_not_count(sim):
    record = install.Record("demo", "SYS:amigent", ["C:"], started="2026-11-21 10:00:00")
    sim.data[record.marker] = b"2026-11-01 09:00:00\n"
    assert not server._marker_ok(record)
    sim.data[record.marker] = b"2026-11-21 10:00:00\n"
    assert server._marker_ok(record)


def test_a_record_from_before_1_5_0_still_undoes(sim):
    old = {"name": "legacy", "home": "SYS:amigent", "paths": ["C:", "LIBS:"],
           "started": "2026-11-01 10:00:00",
           "before": {"C:": {"Dir": entry()}, "LIBS:": {"old.library": entry(500)}}, "missing": [],
           "added": ["LIBS:x.library"], "changed": ["C:Dir"], "removed": [],
           "done": True, "undone": False}
    folder = install.folder_for(server._amiga())
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "legacy.json").write_text(json.dumps(old))
    sim.files["SYS:amigent/undo/legacy/C/Dir"] = entry()  # its whole copy of C:
    sim.files["C:Dir"] = entry(77, "later")
    sim.files["LIBS:x.library"] = entry(3)
    sim.files.pop("C:List")  # not part of the old record's world
    record = install.load(server._amiga(), "legacy")
    assert record.copied is None and record.has_copy("C:Dir")
    assert "as before" in server.amiga_install_undo("legacy")
    assert sim.files["C:Dir"]["size"] == 10 and "LIBS:x.library" not in sim.files


def test_multi_folder_assigns_find_the_file_where_it_is():
    r = install.Record("demo", "SYS:amigent", ["Sys:Libs", "Sys:Classes"],
                       aliases={"LIBS:": ["Sys:Libs", "Sys:Classes"]},
                       before={"Sys:Libs": {"a.library": entry()}, "Sys:Classes": {"b.library": entry()}})
    assert r.locate("LIBS:b.library") == ("Sys:Classes", "b.library")
    assert r.locate("LIBS:new.library") == ("Sys:Libs", "new.library")
    r.copied = ["Sys:Classes/b.library"]
    assert r.has_copy("LIBS:b.library") and not r.has_copy("LIBS:a.library")
    assert install.plan(r, [], ["LIBS:a.library"]) == ["Sys:Libs/a.library"]
    assert install.plan(r, ["LIBS:"], []) == ["Sys:Libs", "Sys:Classes"]


def test_estimate_learns_from_what_copying_took(tmp_path):
    amiga = "test"
    assert install.factor(amiga) == 1.0
    install.learn(amiga, 10.0, 40.0)
    assert install.factor(amiga) == pytest.approx(4.0)
    install.learn(amiga, 10.0, 20.0)
    assert install.factor(amiga) == pytest.approx(3.0)
    install.learn(amiga, 0.5, 400.0)  # too short to tell
    assert install.factor(amiga) == pytest.approx(3.0)
    r = install.Record("demo", "SYS:amigent", ["C:"], before={"C:": {"a": entry(300 * 1024)}})
    est = install.estimate(r, ["C:"], factor=2.0)
    assert est["files"] == 1 and est["bytes"] == 300 * 1024
    assert est["seconds"] == pytest.approx(2 * (install.PER_FILE + 1.0), abs=0.1)


def test_the_backup_script_writes_its_marker_last_and_only_on_success():
    r = install.Record("demo", "SYS:amigent", ["C:", "SYS:Games"], started="t",
                       before={"C:": {"sub": {"type": "DIR"}, "sub/x": entry()}, "SYS:Games": {}})
    r.copied = ["SYS:Games", "C:sub/x"]
    script = install.begin_script(r).splitlines()
    assert script.index('Delete >NIL: "SYS:amigent/undo/demo/complete" QUIET') < script.index(
        'Copy "SYS:Games" "SYS:amigent/undo/demo/SYS_Games" ALL CLONE NOREQ QUIET')
    assert 'MakeDir >NIL: "SYS:amigent/undo/demo/C/sub"' in script
    assert 'Copy "C:sub/x" "SYS:amigent/undo/demo/C/sub/x" CLONE NOREQ QUIET' in script
    marker = script.index('Echo >"SYS:amigent/undo/demo/complete" "t"')
    assert all(script.index(line) < marker for line in script if line.startswith("Copy"))
    assert script.count("  Skip failed") == 2 and script.index("Lab failed") > marker


def test_aminet_install_copies_aside_what_the_archive_overwrites(sim, monkeypatch):
    import io
    import zipfile
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("old.library", b"version 2")
        z.writestr("docs/readme", b"text")
    monkeypatch.setattr(server.aminet, "download", lambda path: archive.getvalue())
    monkeypatch.setattr(server.aminet, "tool_for", lambda name: "")  # unpacked on the PC
    def put(ctl, local, target):
        for f in local.rglob("*"):
            if f.is_file():
                ctl.write_file(join(target, f.relative_to(local).as_posix()), f.read_bytes())
    monkeypatch.setattr(server.files, "put", put)
    server.amiga_install_begin("demo")
    said = server.amiga_aminet_install("util/libs/old.zip", "LIBS:")
    assert "copied 1 file(s) aside for the installation 'demo'" in said
    server.amiga_install_end("demo")
    assert "as before" in server.amiga_install_undo("demo")
    assert sim.files["LIBS:old.library"]["size"] == 500 and "LIBS:docs/readme" not in sim.files
    assert server._archive_members("x.zip", archive.getvalue()) == ["old.library", "docs/readme"]
    assert server._archive_members("x.lzx", b"") is None
