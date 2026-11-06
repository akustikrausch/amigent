"""MCP server that lets an agent operate a real Amiga."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path, PurePosixPath

from mcp.server.fastmcp import FastMCP, Image as McpImage

from . import files, inventory, locks, machines, pictures, secret, uptime, watch
from .client import join_path
from .errors import AmigaError
from .amigentd import Amigentd
from .channel import Commands, Request, perform
from .power import BOOT_WAIT, RESTART_WAIT, power_cycle, set_power, wait_for_port, flush_volumes, reboot, wait_gone
from .power import available as power_available
from .viewer import Viewer, open_window

HOST = machines.pick()  # AMIGENT_HOST (a name or an address), else the known Amiga
CTL_PORT = int(os.environ.get("AMIGENT_CTL_PORT", "6800"))
SCREEN_PORT = int(os.environ.get("AMIGENT_SCREEN_PORT", "6801"))
AMIGA_DIR = os.environ.get("AMIGENT_AMIGA_DIR", "SYS:amigent")
MAX_EDGE = pictures.MAX_EDGE  # larger images get downscaled by the model API anyway
SAME_SHOT = 120.0  # seconds an unchanged region counts as already seen
LOCK_FILE = "amigent.lock"  # lives in the temp folder, see locks.py
VIEWER_PORT = int(os.environ.get("AMIGENT_VIEWER_PORT", "6810"))
FLUSH_VOLUMES = flush_volumes()
SLOW_COMMAND = re.compile(r"^\s*(list|search|dir)\b.*\bALL\b", re.IGNORECASE)

def inventory_folder() -> Path:
    """One shelf per Amiga for its scans, next to keys and certificates."""
    return secret.FOLDER / "inventory" / secret.safe(machines.name_of(HOST) or "Amiga")


mcp = FastMCP("amigent")
ctl = Commands(HOST, CTL_PORT, SCREEN_PORT)  # amigentd's channel, or amigactld on older installs
screen = Amigentd(HOST, SCREEN_PORT)


def _exclusive(name: str = LOCK_FILE):
    """Serialise access across all amigent processes and threads on this PC."""
    return locks.exclusive(name)


def _screen_lock():
    """The screen service has its own lock, so the viewer keeps running during commands."""
    return _exclusive(LOCK_FILE + ".screen")


def _with_ctl(fn):
    """Run fn against amigactld, reconnecting once after a dropped link."""
    with _exclusive():
        if not ctl.connected:
            ctl.connect()
        try:
            return fn()
        except (OSError, EOFError, AmigaError) as e:
            if isinstance(e, AmigaError) and e.code is not None:
                raise  # the daemon answered, this is a real command error
            ctl.close()
            if isinstance(e, TimeoutError):
                # The command reached the Amiga and may still run: repeating it could run it twice.
                raise AmigaError("no answer in time; the command may still be running on the "
                                 "Amiga. Use amiga_job for long work") from e
            try:
                ctl.connect()
            except AmigaError as down:
                raise AmigaError(
                    "amigactld is not answering. It handles one command at a time, so a long "
                    f"command may still be running ({down})"
                ) from down
            return fn()


def _with_screen(fn):
    """Run fn against amigentd; the client reconnects by itself."""
    with _screen_lock():
        return fn()


_join = join_path


seen = uptime.Seen(secret.FOLDER / "uptime.json")


@mcp.tool()
def amiga_status() -> str:
    """Show memory, OS and TCP stack versions, and mounted volumes with free space.

    Also tells whether the Amiga started again since the last look, and the
    guru code it kept from before the last reset, so after a lost connection
    you can tell a crash from a network fault. Call it first when the Amiga
    answers again after being away.
    """
    info = _with_ctl(ctl.sysinfo)
    report = {"amiga": machines.name_of(HOST), "address": machines.address(HOST)}
    if "uptime" in info:
        report["running_for"] = uptime.duration(int(info["uptime"]))
    said = seen.check(machines.name_of(HOST) or machines.address(HOST) or "", info)
    if said:
        report["notes"] = said
    report.update({"sysinfo": info, "volumes": _with_ctl(ctl.volumes)})
    return json.dumps(report, indent=1)


def _clear_hold() -> str:
    """Before a window opens: get Intuition going again, or say what holds it up."""
    try:
        state = _with_screen(screen.held)
    except (OSError, AmigaError):
        return ""
    done = ""
    if state == "held":  # a popup left open, such as Directory Opus's after a short right click
        _with_screen(lambda: screen.press("esc"))
        done = "closed a popup with Esc first. "
    elif state == "menu" and screen.right_held():  # our own right button holds a menu open
        _with_screen(lambda: screen.button("right", False))
        done = "let go of the right mouse button first. "
    else:
        return HOLD_NOTE[state] if state else ""
    time.sleep(0.3)
    state = _with_screen(screen.held)
    return done + (HOLD_NOTE[state] if state else "")


HOLD_NOTE = {
    "menu": "A menu is open on the Amiga (someone holds the right button), so new windows "
            "wait until it closes. ",
    "held": "Intuition holds back new windows, as behind a popup left open; amiga_key('esc') "
            "usually closes it. ",
}


@mcp.tool()
def amiga_find(address: str = "") -> str:
    """Find the Amiga again when it does not answer, for example after its address changed.

    Without an address the home network is searched; Amigent takes only the Amiga
    that proves to be this one (its certificate or key). Pass an address when the
    user knows it. Amigent remembers the result for the remote desktop as well.
    """
    if not HOST:
        return "No Amiga known yet: run amigent-setup."
    before = machines.address(HOST)
    if address:
        machines.move(HOST, address.strip())
        found = address.strip()
    else:
        machines.forget_search(HOST)
        found = machines.relocate(HOST)
    ctl.close()
    screen.close()
    if not found:
        return (f"The Amiga does not answer at {before} and is not elsewhere on this network. "
                "Is it switched on and amigentd running? Ask the user for its address.")
    return f'"{machines.name_of(HOST)}" is at {found} now (was {before}).'


@mcp.tool()
def amiga_exec(command: str, cd: str = "", timeout: float = 30) -> str:
    """Run a short AmigaDOS command and return rc and output.

    Blocks the Amiga daemon for everyone until it returns. Use amiga_job for
    anything that may take long, and amiga_start for graphical programs.
    """
    if SLOW_COMMAND.search(command):
        raise AmigaError("recursive scans can block the Amiga for many minutes; use amiga_job")
    result = _with_ctl(lambda: ctl.exec(command, cd or None, timeout))
    return f"rc={result.rc}\n{result.output}"


last_started = ""


@mcp.tool()
def amiga_start(command: str) -> str:
    """Start a program in the background, without capturing output."""
    global last_started
    before = _clear_hold()
    _with_ctl(lambda: ctl.exec(f"Run >NIL: {command}"))
    last_started = command
    return f"{before}started: {command}"


@mcp.tool()
def amiga_break(command: str = "", signal: str = "C") -> str:
    """Send Ctrl-C (or D, E, F) to a running command, like pressing it in its Shell.

    command is the program's name as Status shows it, e.g. 'List' or 'Wait'.
    Without it, the command last started by amiga_job or amiga_start is meant.
    A plain amiga_exec cannot be broken this way: it blocks the daemon until
    it returns, so long work belongs in amiga_job.
    """
    name = command or last_started
    if not name:
        raise AmigaError("nothing started yet; give the command's name")
    if signal.upper() not in ("C", "D", "E", "F"):
        raise AmigaError("signal must be C, D, E or F")
    wanted = PurePosixPath(name.split()[0].split(":")[-1]).name.lower()
    status = _with_ctl(lambda: ctl.exec("Status")).output
    hits = []
    for line in status.splitlines():
        m = re.match(r"\s*Process\s+(\d+):.*?command:\s*(\S+)", line)
        if m and PurePosixPath(m.group(2).split(":")[-1]).name.lower() == wanted:
            hits.append(m.group(1))
    if not hits:
        return f"no running process named {wanted}"
    for number in hits:
        _with_ctl(lambda: ctl.exec(f"Break {number} {signal.upper()}"))
    return f"sent Ctrl-{signal.upper()} to {wanted} (process {', '.join(hits)})"


@mcp.tool()
def amiga_arexx(program: str, port: str = "") -> str:
    """Run ARexx on the Amiga and return rc and output.

    With port, program is one or more commands for that application's ARexx
    port (see amiga_rexx_ports), one per line, sent exactly as written; the
    RESULT of the last one is printed. Without port, program is a complete
    ARexx script.
    """
    if port:
        # Each line goes to the program as it is: quoted, so ARexx doesn't evaluate it.
        lines = [line.strip() for line in program.strip().splitlines() if line.strip()]
        body = "\n".join("'" + line.replace("'", "''") + "'" for line in lines)
        script = (f"/* amigent */\noptions results\naddress '{port}'\n{body}\n"
                  "if symbol('RESULT') = 'VAR' then say result\nexit rc\n")
    else:
        script = program if program.lstrip().startswith("/*") else f"/* amigent */\n{program}\n"
    # script and rx in one batch: one round trip instead of two
    wrote, result = _with_ctl(lambda: perform(ctl, [
        Request.write("RAM:amigent.rexx", script.encode("iso-8859-1")),
        Request.exec("rx RAM:amigent.rexx", timeout=60)]))
    for answer in (wrote, result):
        if isinstance(answer, AmigaError):
            raise answer
    return f"rc={result.rc}\n{result.output}"


@mcp.tool()
def amiga_rexx_ports() -> str:
    """List the public message ports, i.e. the applications ARexx can talk to."""
    out = amiga_arexx("say show('P',, '0a'x)")
    return out


@mcp.tool()
def amiga_job(command: str, wait: float = 600) -> str:
    """Run a long command at low priority in the background and return its output.

    The Amiga stays reachable meanwhile. If wait runs out, the job keeps going
    and its output ends up in RAM:amigent-job.out.
    """
    script = (
        "FailAt 21\nChangeTaskPri -5\n"
        f"{command} >RAM:amigent-job.out\n"
        'Echo >RAM:amigent-job.done "done"\n'
    )
    answers = _with_ctl(lambda: perform(ctl, [
        Request.exec("Delete >NIL: RAM:amigent-job.done RAM:amigent-job.out QUIET"),
        Request.write("RAM:amigent-job.script", script.encode("iso-8859-1")),
        Request.exec("Run >NIL: Execute RAM:amigent-job.script")]))
    for answer in answers[1:]:
        if isinstance(answer, AmigaError):
            raise answer
    global last_started
    last_started = command
    deadline = time.monotonic() + wait
    pause = 0.5  # short jobs are common: look soon, then back off to 5 s
    while time.monotonic() < deadline:
        # marker and output in one go: a finished job costs one round trip, not two
        done, output = _with_ctl(lambda: perform(ctl, [Request.stat("RAM:amigent-job.done"),
                                                       Request.read_file("RAM:amigent-job.out")]))
        if not isinstance(done, AmigaError):
            if isinstance(output, AmigaError):
                raise output
            return output.decode("iso-8859-1")
        time.sleep(min(pause, max(deadline - time.monotonic(), 0)))
        pause = min(pause * 2, 5)
    return "still running; output will be in RAM:amigent-job.out"


@mcp.tool()
def amiga_list(path: str, recursive: bool = False) -> str:
    """List a directory, e.g. 'Work:Games'. Amiga paths use VOLUME:dir/file."""
    entries = _with_ctl(lambda: ctl.list_dir(path, recursive))
    return "\n".join(
        f"{e['type']:4} {e['size']:>10} {e['date']} {e['name']}" for e in entries
    )


@mcp.tool()
def amiga_read_text(path: str) -> str:
    """Read a text file from the Amiga."""
    return _with_ctl(lambda: ctl.read_file(path)).decode("iso-8859-1")


@mcp.tool()
def amiga_write_text(path: str, text: str) -> str:
    """Create or replace a text file on the Amiga, with LF line endings."""
    _with_ctl(lambda: ctl.write_file(path, text.encode("iso-8859-1")))
    return f"wrote {path}"


@mcp.tool()
def amiga_put(local_path: str, amiga_path: str) -> str:
    """Copy a local file or directory tree to the Amiga.

    amiga_path is the full target name, e.g. 'Work:Games/Foo'. Files the Amiga
    already has with the same size and CRC are left alone, so putting a tree
    again only sends what changed.
    """
    src = Path(local_path).expanduser()
    return _with_ctl(lambda: files.put(ctl, src, amiga_path))


@mcp.tool()
def amiga_get(amiga_path: str, local_path: str) -> str:
    """Copy a file or a whole directory tree from the Amiga to the local machine.

    Local files with the same size and CRC are not fetched again.
    """
    return _with_ctl(lambda: files.get(ctl, amiga_path, Path(local_path).expanduser()))


@mcp.tool()
def amiga_mkdir(path: str) -> str:
    """Create a directory, including missing parents."""
    _with_ctl(lambda: files.makedirs(ctl, path))
    return f"created {path}"


@mcp.tool()
def amiga_move(source: str, dest: str) -> str:
    """Rename or move a file within one volume."""
    _with_ctl(lambda: ctl.rename(source, dest))
    return f"moved {source} to {dest}"


@mcp.tool()
def amiga_delete(path: str) -> str:
    """Delete one file or one empty directory. There is no undo."""
    _with_ctl(lambda: ctl.delete(path))
    return f"deleted {path}"


@mcp.tool()
def amiga_inventory(max_wait: float = 1800) -> str:
    """Scan installed programs and versions on the Amiga; can take many minutes.

    Saves JSON under inventory/ and reports differences to the previous scan.
    """
    _with_ctl(lambda: ctl.exec(f"Run >NIL: Execute {AMIGA_DIR}/amigent-scan"))
    time.sleep(5)  # let the script remove the previous marker
    deadline = time.monotonic() + max_wait
    while True:
        try:
            _with_ctl(lambda: ctl.stat("RAM:amigent-scan.done"))
            break
        except AmigaError:
            if time.monotonic() > deadline:
                raise AmigaError("scan did not finish in time") from None
            time.sleep(10)
    text = _with_ctl(lambda: ctl.read_file("RAM:amigent-inventory.txt")).decode("iso-8859-1")
    items = inventory.parse(text)
    folder = inventory_folder()
    folder.mkdir(parents=True, exist_ok=True)
    scans = sorted(folder.glob("*.json"))
    previous = json.loads(scans[-1].read_text(encoding="utf-8")) if scans else None
    target = folder / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    target.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
    summary = {"file": str(target), "files": len(items),
               "with_version": sum(1 for i in items.values() if i["version"])}
    if previous is not None:
        summary["diff"] = inventory.diff(previous, items)
    return json.dumps(summary, indent=1, ensure_ascii=False)


@mcp.tool()
def amiga_screenshot(x: int = 0, y: int = 0, width: int = 0, height: int = 0) -> list:
    """Capture the Amiga screen, or a region of it to read small text.

    Transfers only what changed since the last call, so repeated shots are
    quick. Colours are reduced to 256. All coordinates for the pointer tools
    are screen pixels; use the scale in the reply for reduced images. If the
    same region has not changed since your last shot of it, only a note comes
    back; ask again right away to get the picture anyway.
    """
    global last_look, _last_shot
    if paused := _paused_note():
        return [paused]
    region = (x, y, width, height)
    image = _with_screen(lambda: screen.capture(x, y, width or None, height or None))
    seen, last_look = last_look, time.monotonic()  # after the fetch: what it brought is what was seen
    note = _same_shot(region, seen)
    _last_shot = (region, last_look, bool(note))
    if note:
        return [note]
    size = image.size
    image, scale = pictures.fit(image)
    note = (
        f"screen {screen.width}x{screen.height}; region {size[0]}x{size[1]} at ({x},{y}); "
        f"screen_x = {x} + image_x * {scale:.3f}, "
        f"screen_y = {y} + image_y * {scale:.3f}"
    )
    return [note, McpImage(data=pictures.png(image), format="png")]


_last_shot: tuple | None = None  # (region, when, answered without a picture)


def _same_shot(region: tuple, seen: float) -> str:
    """A note instead of a picture when the model already holds this very picture.

    Same region, recent, no tile in it since, and the last answer was a real
    picture: a second ask in a row always gets one."""
    if not _last_shot or _last_shot[0] != region or _last_shot[2]:
        return ""
    when = _last_shot[1]
    if seen != when or time.monotonic() - when > SAME_SHOT:
        return ""  # someone looked elsewhere meanwhile, or it's been a while
    x, y, w, h = region
    if screen.changed_since(when, x, y, w or None, h or None):
        return ""
    return (f"no change in this region since your screenshot {time.monotonic() - when:.0f} s "
            "ago, so no new picture; call again to get it anyway")


@mcp.tool()
def amiga_screenshot_color(x: int, y: int, width: int, height: int) -> list:
    """A region in full colour, for small text or exact colours.

    amiga_screenshot is cheaper and covers the whole screen in 256 colours;
    use this one for the part you need to read precisely.
    """
    global last_look
    if paused := _paused_note():
        return [paused]
    image = _with_screen(lambda: screen.capture_rgb(x, y, width, height))
    last_look = time.monotonic()  # after the fetch: what it brought is what was seen
    return [f"region {width}x{height} at ({x},{y}), full colour",
            McpImage(data=pictures.png(image, exact=True), format="png")]


@mcp.tool()
def amiga_wait_change(x: int = 0, y: int = 0, width: int = 0, height: int = 0,
                      timeout: float = 30) -> str:
    """Wait until a region (or the screen) differs from what you last looked at.

    Counts from your last screenshot, so a change your click already caused is
    reported at once. Use it after a click or command instead of taking
    screenshots in a loop; no pictures are sent. Returns within timeout even
    on a slow link.
    """
    global last_look
    if paused := _paused_note():
        return paused
    # Tiles count whoever fetched them: an open viewer may have taken them first.
    report, changed = watch.wait_change(screen, _with_screen, lookout,
                                        (x, y, width or None, height or None),
                                        last_look or time.monotonic(), timeout)
    if changed:
        last_look = time.monotonic()
    return report


lookout = watch.Lookout()  # screen polls that a deadline can walk away from


@mcp.tool()
def amiga_pause_screen(seconds: int = 300) -> str:
    """Stop the Amiga reading its screen for a while, so a test or benchmark has its CPU.

    Until then nothing reads the screen, whoever asks: the viewer keeps its
    last picture, screenshots and wait_change say the screen is paused.
    Mouse, keys, commands, jobs and files work as usual. A new call replaces
    the running pause, 0 ends it, at most 65535 s. Programs on the Amiga can
    do the same through ARexx: address AMIGENT 'PAUSE 300', then 'RESUME'.
    """
    try:
        left = _with_ctl(lambda: ctl.pause_screen(seconds))
    except AttributeError:
        raise AmigaError("pausing the screen needs amigentd's command channel; run amigent-setup --update") from None
    screen.paused_until = time.monotonic() + left if left else 0.0
    return f"screen paused for {left} s; amiga_pause_screen(0) ends it" if left else "screen pause ended"


def _paused_note() -> str:
    """While the Amiga pauses its screen, the note that stands in for a picture."""
    left = _with_screen(screen.pause_left)
    if not left:
        return ""
    return (f"the screen is paused on the Amiga for another {left} s, so no picture is read; "
            "mouse, keys and commands still work. amiga_pause_screen(0) ends the pause")


@mcp.tool()
def amiga_screens() -> str:
    """List all Intuition screens, front to back, with their handles."""
    found = _with_screen(screen.screens)
    return "\n".join(
        f"{s['handle']:#010x} {s['width']}x{s['height']}x{s['depth']}"
        f"{' front' if s['front'] else ''}{' workbench' if s['workbench'] else ''}"
        f"{' ham' if s['ham'] else ''} {s['title']!r}" for s in found)


@mcp.tool()
def amiga_screen_front(screen_id: str) -> str:
    """Bring a screen to the front, by handle from amiga_screens or by part of its title."""
    found = _with_screen(screen.screens)
    try:
        handle = int(screen_id, 0)
        target = next(s for s in found if s["handle"] == handle)
    except (ValueError, StopIteration):
        matches = [s for s in found if screen_id.lower() in s["title"].lower()]
        if len(matches) != 1:
            raise AmigaError(f"{len(matches)} screens match {screen_id!r}; use the handle") from None
        target = matches[0]
    if not _with_screen(lambda: screen.screen_to_front(target["handle"])):
        raise AmigaError("that screen has just closed")
    _with_screen(screen.check_size)
    return f"front screen is now {target['title']!r} ({target['width']}x{target['height']})"


@mcp.tool()
def amiga_mouse_relative(dx: int, dy: int) -> str:
    """Move the mouse by a distance, as a real mouse does.

    For games and other programs that read mouse movement and keep their own
    pointer; amiga_click places the Intuition pointer, which those ignore.
    """
    _with_screen(lambda: screen.move_relative(dx, dy))
    return f"moved by ({dx},{dy})"


@mcp.tool()
def amiga_ui_tree(pictures: bool = True) -> list:
    """The front screen's windows and gadgets as text, with labels, boxes and numbers.

    Labels come from classic Intuition gadgets and GadTools checkboxes, strings
    and sliders. Gadgets whose label can't be read (GadTools buttons, BOOPSI,
    ReAction, MUI, requesters patched by tools like ReqAttack) come with a
    picture: each one cut out of the screen next to its #number, so you can
    read what it says and click it with amiga_ui_click(number=..., window=...).
    Only the active window's gadgets get pictures.
    """
    tree = _with_screen(screen.ui_tree)
    lines = []
    for item in tree:
        box = f"({item.get('x', 0)},{item.get('y', 0)} {item.get('w', item.get('width', 0))}x" \
              f"{item.get('h', item.get('height', 0))})"
        if item["kind"] == "screen":
            lines.append(f"screen {item['title']!r} {item['width']}x{item['height']}")
        elif item["kind"] == "window":
            lines.append(f"  window {item['id']} {item['title']!r} {box}{' active' if item['active'] else ''}")
        else:
            state = " disabled" if item["disabled"] else ""
            state += " selected" if item["selected"] else ""
            lines.append(f"    #{item['id']} {item['type']} {item['label']!r} {box}{state}")
    text = "\n".join(lines) or "no windows"
    hold = next((i.get("held", "") for i in tree if i["kind"] == "screen"), "")
    if hold:
        text = f"note: {HOLD_NOTE[hold]}\n{text}"
    sheet = _gadget_sheet(tree) if pictures else None
    return [text, sheet] if sheet else [text]


def _gadget_sheet(tree: list):
    """One picture of the active window's unlabelled gadgets, each beside its number."""
    gadgets = pictures.unlabelled(tree)
    if not gadgets:
        return None
    box = pictures.bounds(gadgets)  # fetch the gadgets' corner of the screen, not all of it
    picture = _with_screen(lambda: screen.capture(*box))
    sheet = pictures.gadget_sheet(gadgets, picture, box[:2])
    return McpImage(data=pictures.png(sheet), format="png")


def _centre(item: dict) -> tuple[int, int]:
    return item["x"] + item["w"] // 2, item["y"] + item["h"] // 2


@mcp.tool()
def amiga_ui_click(label: str = "", window: str = "", number: int = -1, button: str = "left",
                   double: bool = False) -> str:
    """Click a gadget from amiga_ui_tree by its label, or by its #number within a window.

    window is part of the window's title, or its id from amiga_ui_tree.
    """
    tree = _with_screen(screen.ui_tree)
    windows = {w["id"]: w["title"] for w in tree if w["kind"] == "window"}
    gadgets = [g for g in tree if g["kind"] == "gadget"
               and (not window or window == g["window"]
                    or window.lower() in windows.get(g["window"], "").lower())]
    if number >= 0:
        matches = [g for g in gadgets if g["id"] == str(number)]
    else:
        if not label:
            raise AmigaError("give a label or a number")
        exact = [g for g in gadgets if g["label"].lower() == label.lower()]
        matches = exact or [g for g in gadgets if label.lower() in g["label"].lower()]
    if len(matches) != 1:
        names = ", ".join(repr(g["label"]) for g in matches[:8])
        raise AmigaError(f"{len(matches)} gadgets match {label!r}{': ' + names if names else ''}")
    gadget = matches[0]
    if gadget["disabled"]:
        raise AmigaError(f"{gadget['label']!r} is disabled")
    x, y = _centre(gadget)
    _with_screen(lambda: screen.click(x, y, button, double))
    return f"clicked #{gadget['id']} {gadget['label']!r} at ({x},{y})"


@mcp.tool()
def amiga_menus() -> str:
    """The active window's menus and items, with shortcuts and state."""
    lines = []
    for m in _with_screen(screen.menus):
        flags = ("" if m["enabled"] else " disabled") + (" checked" if m["checked"] else "")
        key = f" [Amiga {m['key']}]" if m.get("key") else ""
        indent = {"menu": "", "item": "  ", "sub": "    "}[m["kind"]]
        lines.append(f"{indent}{m['text']}{key}{flags}")
    return "\n".join(lines) or "the active window has no menus"


@mcp.tool()
def amiga_menu_select(path: str) -> str:
    """Pick a menu item by name, e.g. 'Project/Save as...' or 'Window/Show/All files'.

    Holds the right button, moves over the menu and lets go on the item,
    the way a person does it.
    """
    parts = [p.strip().lower() for p in path.split("/") if p.strip()]
    entries = _with_screen(screen.menus)

    def find(kind, prefix, name):
        hits = [e for e in entries if e["kind"] == kind and e["path"][:len(prefix)] == prefix
                and e["text"].strip().lower() == name]
        hits = hits or [e for e in entries if e["kind"] == kind and e["path"][:len(prefix)] == prefix
                        and e["text"].strip().lower().startswith(name)]
        if not hits:
            raise AmigaError(f"no menu entry {name!r}; see amiga_menus")
        return hits[0]

    chain, prefix = [], []
    for kind, name in zip(("menu", "item", "sub"), parts):
        entry = find(kind, prefix, name)
        if not entry["enabled"]:
            raise AmigaError(f"{entry['text']!r} is disabled")
        chain.append(entry)
        prefix = entry["path"]
    if len(chain) < 2:
        raise AmigaError("give at least menu/item")

    def pick():
        x, y = _centre(chain[0])
        screen.move(x, y)
        time.sleep(0.05)
        screen.button("right", True)
        time.sleep(0.15)
        for entry in chain[1:]:
            screen.move(*_centre(entry))
            time.sleep(0.15)
        screen.button("right", False)

    _with_screen(pick)
    return f"picked {' / '.join(e['text'] for e in chain)}"


@mcp.tool()
def amiga_click(x: int, y: int, button: str = "left", double: bool = False) -> str:
    """Click at screen pixels. button is left, right or middle.

    The right button opens menus only while held; use amiga_menu_select or
    amiga_drag for menus. Avoid short right clicks: on a Directory Opus desktop
    they leave a popup open that holds back every new window and screen until
    Esc is pressed (amiga_key('esc')).
    """
    _with_screen(lambda: screen.click(x, y, button, double))
    return f"clicked {button} at ({x},{y})"


@mcp.tool()
def amiga_mouse_move(x: int, y: int) -> str:
    """Move the pointer to screen pixels without clicking, e.g. to show a tooltip."""
    _with_screen(lambda: screen.move(x, y))
    return f"pointer at ({x},{y})"


@mcp.tool()
def amiga_button(button: str = "left", down: bool = True) -> str:
    """Press (down=True) or release (down=False) one mouse button on its own.

    For long presses and drags with stops on the way; release it again afterwards.
    """
    if button not in ("left", "right", "middle"):
        raise AmigaError("button must be left, right or middle")
    _with_screen(lambda: screen.button(button, down))
    return f"{button} button {'down' if down else 'up'}"


@mcp.tool()
def amiga_drag(x1: int, y1: int, x2: int, y2: int, button: str = "left") -> str:
    """Press at one point and release at another: drag icons or pick menu items."""
    _with_screen(lambda: screen.drag(x1, y1, x2, y2, button))
    return f"dragged {button} from ({x1},{y1}) to ({x2},{y2})"


SETTLE_FIRST = 0.3  # after Return, before the window counts: the Shell starts the command
SETTLE_QUIET = 0.6  # this long without a change and the window has settled


def _settle(box: tuple, wait: float) -> bool:
    """Wait until box has not changed for SETTLE_QUIET s, at most wait; True if it did.

    One changed tile is a blinking cursor, not output."""
    end = time.monotonic() + wait
    time.sleep(SETTLE_FIRST)
    quiet_since = time.monotonic()
    while time.monotonic() < end:
        if screen.poll(*box) > 1:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= SETTLE_QUIET:
            return True
        time.sleep(0.15)
    return False


@mcp.tool()
def amiga_type(text: str, settle: float = 10) -> str:
    """Type text into the active window through its keymap. Newline presses Return.

    Several lines go in one at a time: after each Return it waits, up to settle
    seconds, until the window has been still for a moment, so a Shell is done
    with one command before the next one comes. A command that runs quietly
    for long still gets the next line early, into the Shell's type-ahead.
    settle=0 types everything at once.
    """
    lines = text.splitlines(keepends=True)
    if settle <= 0 or len(lines) < 2:
        _with_screen(lambda: screen.type_text(text))
        return f"typed {len(text)} characters"

    def run():
        box = next(((w["x"], w["y"], w["w"], w["h"]) for w in screen.ui_tree()
                    if w["kind"] == "window" and w["active"]), None)
        late = 0
        for i, line in enumerate(lines):
            screen.type_text(line)
            if box and line.endswith("\n") and i < len(lines) - 1 and not _settle(box, settle):
                late += 1
        return late

    late = _with_screen(run)
    note = f"; {late} line(s) went on before the window was still" if late else ""
    return f"typed {len(text)} characters in {len(lines)} lines{note}"


@mcp.tool()
def amiga_key(combo: str) -> str:
    """Press a key or combination: 'return', 'esc', 'lamiga+n', 'ctrl+c', 'f1', 'help'."""
    _with_screen(lambda: screen.press(combo))
    return f"pressed {combo}"


viewer: Viewer | None = None
last_look = 0.0  # when the agent last looked at the screen; wait_change counts from here


@mcp.tool()
def amiga_viewer() -> str:
    """Open a live window of the Amiga screen on the user's desktop for manual control.

    Use when the user wants to see the Amiga or drive it themselves: mouse,
    keyboard, Amiga key shortcuts, file upload. Your own screenshots and
    clicks keep working meanwhile and show up in the window.
    """
    global viewer
    if viewer is None:
        viewer = Viewer(HOST, screen=screen, lock=_screen_lock, ctl_port=CTL_PORT,
                        restart=amiga_restart, flush_volumes=FLUSH_VOLUMES, remember=True)
    url = viewer.start(VIEWER_PORT)
    open_window(url)
    return f"viewer window opened: {url}"


@mcp.tool()
def amiga_restart() -> str:
    """Restart cleanly through the system and wait until the Amiga answers.

    Some Amigas come back without network after a warm restart. With power
    control set up, the power is cycled after two minutes without an answer;
    the disks were written before the restart, so nothing is lost.
    """
    with _exclusive():
        reboot(ctl, FLUSH_VOLUMES)
        with _screen_lock():
            screen.close()
        if not wait_gone(HOST, SCREEN_PORT):
            raise AmigaError("the Amiga is still up: C:Reboot did not restart it")
        time.sleep(5)
        # amigentd greets even before a key
        if wait_for_port(HOST, SCREEN_PORT, RESTART_WAIT if power_available() else BOOT_WAIT):
            return "Amiga restarted"
        if not power_available():
            raise AmigaError("no answer after restart; amiga_power('cycle') may be needed")
        attempts = power_cycle(HOST, SCREEN_PORT)
    return f"Amiga restarted; it came back without network, so the power was cycled ({attempts}x)"


@mcp.tool()
def amiga_power(action: str) -> str:
    """Control mains power, if configured: 'on', 'off' or 'cycle'.

    Last resort for a hung Amiga: cutting power loses file changes from the
    last minutes. Prefer amiga_restart while the Amiga still answers.
    """
    action = action.strip().lower()
    if action not in ("on", "off", "cycle"):
        raise AmigaError("action must be 'on', 'off' or 'cycle'")
    if action == "cycle":
        with _exclusive():
            ctl.close()
            with _screen_lock():
                screen.close()
            attempts = power_cycle(HOST, SCREEN_PORT)
        return f"Amiga is back after {attempts} power cycle(s)"
    set_power(action == "on")
    return f"power {action}"


def main() -> None:
    if not HOST:
        raise SystemExit("no Amiga known yet: run amigent-setup, or set AMIGENT_HOST")
    mcp.run()


if __name__ == "__main__":
    main()
