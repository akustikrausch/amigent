"""Installations that can be taken back.

begin() takes stock of the folders an installer may touch (TREE, with CRCs)
and copies them aside on the Amiga itself, where copying costs no line time.
end() takes stock again: what came, what changed, what went. The copies of
unchanged files go, the rest stays for undo(); the new files go into the
program store and a repair module puts them back after a reinstall of the
system. undo() removes what came and copies back what changed or went.

The record lives on the PC (JSON per Amiga and installation) and as text next
to the copies on the Amiga.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import secret
from .errors import AmigaError

SYSTEM = ["C:", "LIBS:", "DEVS:", "S:", "L:", "ENVARC:", "SYS:WBStartup", "SYS:Classes"]
CRC_LIMIT = 4 * 2**20  # files above this are compared by size and date only: CRC costs disk time


def safe(path: str) -> str:
    """A folder name for an Amiga path: 'SYS:WBStartup' -> 'SYS_WBStartup'."""
    return path.replace(":", "_").replace("/", "_").strip("_") or "root"


def join(folder: str, name: str) -> str:
    return folder + name if folder.endswith((":", "/")) else f"{folder}/{name}"


def name_ok(name: str) -> str:
    name = name.strip()
    if not name or any(c in name for c in ':/"*?#') or len(name) > 60:
        raise AmigaError("an installation needs a plain name, e.g. 'protracker'")
    return name


@dataclass
class Record:
    name: str
    home: str  # where on the Amiga copies, apps and repair modules live
    paths: list[str]
    started: str = ""
    before: dict = field(default_factory=dict)  # path -> {relative name: entry}
    missing: list[str] = field(default_factory=list)  # paths that did not exist before
    added: list[str] = field(default_factory=list)  # full Amiga paths
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    done: bool = False
    undone: bool = False

    @property
    def store(self) -> str:
        return join(join(self.home, "undo"), self.name)

    def backup_of(self, path: str) -> str:
        return join(self.store, safe(path))

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=1, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Record":
        return cls(**json.loads(text))


def folder_for(amiga: str) -> Path:
    return secret.FOLDER / "installs" / secret.safe(amiga or "Amiga")


def load(amiga: str, name: str) -> Record:
    path = folder_for(amiga) / f"{name_ok(name)}.json"
    try:
        return Record.from_json(path.read_text(encoding="utf-8"))
    except OSError:
        raise AmigaError(f"no installation {name!r} recorded for this Amiga") from None


def save(amiga: str, record: Record) -> Path:
    folder = folder_for(amiga)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{record.name}.json"
    path.write_text(record.to_json(), encoding="utf-8")
    return path


def listing(amiga: str) -> list[Record]:
    out = []
    for path in sorted(folder_for(amiga).glob("*.json")):
        try:
            out.append(Record.from_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            continue
    return out


def snapshot(ctl, path: str) -> dict | None:
    """{relative name: entry} of everything below path; None when path isn't there."""
    try:
        entries = ctl.tree(path, crc=True)
    except AmigaError as e:
        if e.code is None:
            raise
        return None
    return {e["name"]: e for e in entries if e["type"] in ("FILE", "DIR")}


def differs(a: dict, b: dict) -> bool:
    if a["type"] != b["type"]:
        return True
    if a["type"] == "DIR":
        return False
    if a["size"] != b["size"]:
        return True
    if a.get("crc") is not None and b.get("crc") is not None and a["size"] <= CRC_LIMIT:
        return a["crc"] != b["crc"]
    return a["date"] != b["date"]


def compare(before: dict | None, after: dict | None) -> tuple[list[str], list[str], list[str]]:
    """Relative names: added, changed, removed."""
    before, after = before or {}, after or {}
    added = sorted(n for n in after if n not in before)
    removed = sorted(n for n in before if n not in after)
    changed = sorted(n for n in after if n in before and differs(before[n], after[n]))
    return added, changed, removed


def begin_script(record: Record) -> str:
    """AmigaDOS that copies the watched folders aside, CLONE keeps dates and bits."""
    lines = ["FailAt 21", f'MakeDir >NIL: "{record.home}"', f'MakeDir >NIL: "{join(record.home, "undo")}"',
             f'MakeDir >NIL: "{record.store}"']
    for path in record.paths:
        if path in record.missing:
            continue
        lines.append(f'Copy "{path}" "{record.backup_of(path)}" ALL CLONE QUIET')
    lines.append(f'Echo >"{join(record.store, "begun")}" "{record.started}"')
    return "\n".join(lines) + "\n"


def prune_script(record: Record) -> str:
    """Deletes the copies that are no longer needed: everything but changed and removed."""
    keep = set(record.changed) | set(record.removed)
    lines = ["FailAt 21"]
    for path, entries in record.before.items():
        for rel, entry in sorted(entries.items(), key=lambda kv: -kv[0].count("/")):
            full = join(path, rel)
            if entry["type"] == "FILE" and full not in keep:
                lines.append(f'Delete >NIL: "{join(record.backup_of(path), rel)}" QUIET')
    return "\n".join(lines) + "\n"


def apps_script(record: Record) -> str:
    """Copies of the new files into the program store, for the repair module."""
    lines = ["FailAt 21", f'MakeDir >NIL: "{join(record.home, "apps")}"']
    base = join(join(record.home, "apps"), record.name)
    lines.append(f'MakeDir >NIL: "{base}"')
    for full in record.added:
        lines.append(f'Copy "{full}" "{join(base, safe(full))}" CLONE QUIET')
    return "\n".join(lines) + "\n"


def repair_module(record: Record, files: dict[str, str]) -> str:
    """A repair module that puts each added file back when it is missing."""
    base = join(join(record.home, "apps"), record.name)
    lines = [f"; {record.name}: written by Amigent on {time.strftime('%Y-%m-%d')} after the "
             "installation, from what it added", ""]
    for full, kind in files.items():
        if kind != "FILE":
            continue
        lines += [f'If NOT EXISTS "{full}"',
                  f'  Copy "{join(base, safe(full))}" "{full}" CLONE QUIET',
                  "EndIf"]
    return "\n".join(lines) + "\n"


def undo_script(record: Record, dirs_added: list[str]) -> str:
    """Removes what came (files first, then folders, deepest first) and copies back the rest."""
    lines = ["FailAt 21"]
    for full in sorted((f for f in record.added if f not in dirs_added), key=lambda f: -f.count("/")):
        lines.append(f'Delete >NIL: "{full}" QUIET')
    for full in sorted(dirs_added, key=lambda f: -f.count("/")):
        lines.append(f'Delete >NIL: "{full}" QUIET')
    for full in record.changed + record.removed:
        for path in record.paths:
            prefix = join(path, "")
            if full.startswith(prefix) or full == path:
                rel = full[len(prefix):]
                lines.append(f'Copy "{join(record.backup_of(path), rel)}" "{full}" CLONE QUIET')
                break
    return "\n".join(lines) + "\n"
