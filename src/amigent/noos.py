"""Programs that switch the operating system off, such as WHDLoad installs (#2).

From the moment such a program takes over, amigentd, the TCP/IP stack and the
network are gone until it quits. To the PC that looks like a crash. When a
command that starts one goes out, a small note is kept per Amiga under the
config folder, so the MCP server and the remote desktop (separate processes)
both can say what is going on instead of reporting a fault.
"""

from __future__ import annotations

import json
import re
import time

from . import paths

KEEP = 12 * 3600  # s: a note older than this is forgotten
SETTLE = 600  # s: answering this long after the start without ever going away, it didn't take over
# s: away at least this long before an answer counts as "it quit". A WHDLoad splash
# can starve the network for a few seconds before the game takes over; that
# hiccup must not clear the note.
AWAY = 30

# The raw key codes a slave names as its quit key, for the ones people meet.
RAWKEYS = {0x45: "Esc", 0x46: "Del", 0x5F: "Help", 0x5D: "numeric pad *", 0x5A: "numeric pad (",
           0x5B: "numeric pad )", 0x5C: "numeric pad /", 0x4A: "numeric pad -", 0x5E: "numeric pad +",
           0x43: "Enter", 0x44: "Return", 0x40: "Space", 0x0F: "numeric pad 0"}
RAWKEYS.update({0x50 + i: f"F{i + 1}" for i in range(10)})

_SLAVE = re.compile(r"""("[^"]+\.slave"|[^\s"=]+\.slave(?=[\s"]|$))""", re.I)
_WHDLOAD = re.compile(r"(^|[\s/:\"])whdload(\.\w+)?(\s|\"|$)", re.I)


def detect(command: str) -> dict | None:
    """What a command starts, if it switches the OS off; None otherwise.

    WHDLoad itself or a .slave file. Anything else (a demo that bangs the
    hardware) can't be told from its command line."""
    slave = _SLAVE.search(command)
    if not slave and not _WHDLOAD.search(command):
        return None
    path = slave.group(1).strip('"') if slave else ""
    name = path.replace(":", "/").rsplit("/", 1)[-1][:-6] if path else ""
    return {"program": f"WHDLoad: {name}" if name else "WHDLoad", "slave": path}


def key_name(rawkey: int | None) -> str:
    if rawkey is None:
        return ""
    return RAWKEYS.get(rawkey, f"raw key ${rawkey:02X}")


def slave_quit_key(data: bytes) -> int | None:
    """ws_keyexit from the start of a slave file: the byte 27 after "WHDLOADS"
    (the header is ws_Security, "WHDLOADS", ws_Version, ...; keyexit at 31)."""
    at = data.find(b"WHDLOADS")
    if at < 4 or at + 27 >= len(data):
        return None
    key = data[at + 27]
    return key or None


def _file(host: str):
    from . import machines
    return paths.FOLDER / "noos" / f"{paths.safe(machines.identity(host) or host)}.json"


def mark(host: str, found: dict, quit_key: int | None = None) -> dict:
    note = {"program": found["program"], "slave": found.get("slave", ""), "since": time.time(),
            "quit_key": key_name(quit_key) if quit_key is not None else "", "gone": False}
    path = _file(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(note), encoding="utf-8")
    return note


def current(host: str) -> dict | None:
    """The note for this Amiga, if one is kept and still young enough."""
    path = _file(host)
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - note.get("since", 0) > KEEP:
        clear(host)
        return None
    return note


def gone(host: str) -> dict | None:
    """The Amiga stopped answering: if a note is kept, mark it seen gone and give it."""
    note = current(host)
    if note and not note.get("gone"):
        note["gone"] = True
        note["gone_at"] = time.time()
        _save(host, note)
    return note


def _save(host: str, note: dict) -> None:
    try:
        _file(host).write_text(json.dumps(note), encoding="utf-8")
    except OSError:
        pass


def answered(host: str) -> None:
    """amigentd answered: a program that took over has quit, or one never did.

    Away for less than AWAY seconds is a hiccup before the takeover, not a quit."""
    note = current(host)
    if not note:
        return
    now = time.time()
    if note.get("gone") and now - note.get("gone_at", 0) >= AWAY:
        clear(host)
    elif now - note.get("since", 0) > SETTLE:
        clear(host)
    elif note.get("gone"):  # only a short break, as while a splash loads: still starting
        note["gone"] = False
        note.pop("gone_at", None)
        _save(host, note)


def clear(host: str) -> None:
    try:
        _file(host).unlink()
    except OSError:
        pass


def explain(note: dict) -> str:
    since = time.strftime("%H:%M", time.localtime(note["since"]))
    key = note.get("quit_key")
    how = (f"its quit key is {key}" if key else
           "the quit key is usually F10, or as the install's ReadMe says")
    return (f"the Amiga runs a program without the operating system ({note['program']}, started "
            f"{since}). amigentd and the network are off until it quits; {how}, pressed on the "
            "Amiga's own keyboard (Amigent cannot send keys meanwhile). Nothing is wrong: don't "
            "cut the power, wait for the Amiga to answer again")
