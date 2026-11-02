"""MCP server that lets an agent operate a real Amiga."""

from __future__ import annotations

import io
import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from mcp.server.fastmcp import FastMCP, Image as McpImage

from . import inventory
from .amigactl import AmigaCtl, AmigaError
from .amigentd import Amigentd
from .power import power_cycle, set_power, wait_for_port
from .viewer import Viewer, open_window

HOST = os.environ.get("AMIGENT_HOST", "")
CTL_PORT = int(os.environ.get("AMIGENT_CTL_PORT", "6800"))
SCREEN_PORT = int(os.environ.get("AMIGENT_SCREEN_PORT", "6801"))
AMIGA_DIR = os.environ.get("AMIGENT_AMIGA_DIR", "SYS:amigent")
MAX_EDGE = 1568  # larger images get downscaled by the model API anyway
LOCK_FILE = "/tmp/amigent.lock"
VIEWER_PORT = int(os.environ.get("AMIGENT_VIEWER_PORT", "6810"))
FLUSH_VOLUMES = os.environ.get("AMIGENT_FLUSH_VOLUMES", "SYS:").split()
SLOW_COMMAND = re.compile(r"^\s*(list|search|dir)\b.*\bALL\b", re.IGNORECASE)
INVENTORY_DIR = Path(__file__).resolve().parents[2] / "inventory"

mcp = FastMCP("amigent")
ctl = AmigaCtl(HOST, CTL_PORT)
screen = Amigentd(HOST, SCREEN_PORT)


@contextmanager
def _exclusive(name: str = LOCK_FILE):
    """Serialise access across all amigent processes and threads on this PC."""
    with open(name, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _screen_lock():
    """The screen service has its own lock, so the viewer keeps running during commands."""
    return _exclusive(LOCK_FILE + ".screen")


def _with_ctl(fn):
    """Run fn against amigactld, reconnecting once after a dropped link."""
    with _exclusive():
        if not ctl._sock:
            ctl.connect()
        try:
            return fn()
        except (OSError, EOFError, AmigaError) as e:
            if isinstance(e, AmigaError) and e.code is not None:
                raise  # the daemon answered, this is a real command error
            ctl.close()
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


def _join(parent: str, name: str) -> str:
    return parent + name if parent.endswith((":", "/")) else f"{parent}/{name}"


@mcp.tool()
def amiga_status() -> str:
    """Show memory, OS and TCP stack versions, and mounted volumes with free space."""
    return json.dumps(
        {"sysinfo": _with_ctl(ctl.sysinfo), "volumes": _with_ctl(ctl.volumes)}, indent=1
    )


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
    _with_ctl(lambda: ctl.exec(f"Run >NIL: {command}"))
    last_started = command
    return f"started: {command}"


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
    port (see amiga_rexx_ports) and its RESULT is printed. Without port,
    program is a complete ARexx script.
    """
    if port:
        lines = program.strip().splitlines()
        body = "\n".join(lines[:-1] + [f"{lines[-1]}", "if symbol('RESULT') = 'VAR' then say result"])
        script = f"/* amigent */\noptions results\naddress '{port}'\n{body}\nexit rc\n"
    else:
        script = program if program.lstrip().startswith("/*") else f"/* amigent */\n{program}\n"
    _with_ctl(lambda: ctl.write_file("RAM:amigent.rexx", script.encode("iso-8859-1")))
    result = _with_ctl(lambda: ctl.exec("rx RAM:amigent.rexx", timeout=60))
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
    _with_ctl(lambda: ctl.exec("Delete >NIL: RAM:amigent-job.done RAM:amigent-job.out QUIET"))
    _with_ctl(lambda: ctl.write_file("RAM:amigent-job.script", script.encode("iso-8859-1")))
    _with_ctl(lambda: ctl.exec("Run >NIL: Execute RAM:amigent-job.script"))
    global last_started
    last_started = command
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            _with_ctl(lambda: ctl.stat("RAM:amigent-job.done"))
            return _with_ctl(lambda: ctl.read_file("RAM:amigent-job.out")).decode("iso-8859-1")
        except AmigaError as e:
            if e.code is None:
                raise
        time.sleep(5)
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

    amiga_path is the full target name, e.g. 'Work:Games/Foo'.
    """
    src = Path(local_path).expanduser()
    files = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
    if not src.exists():
        raise AmigaError(f"{src} does not exist")
    if src.is_dir():
        _makedirs(amiga_path)
    for f in files:
        rel = f.relative_to(src) if src.is_dir() else None
        target = amiga_path
        if rel:
            if len(rel.parts) > 1:
                _makedirs(_join(amiga_path, rel.parent.as_posix()))
            target = _join(amiga_path, rel.as_posix())
        _with_ctl(lambda: ctl.write_file(target, f.read_bytes()))
    return f"copied {len(files)} file(s) to {amiga_path}"


def _makedirs(path: str) -> None:
    volume, _, rest = path.rpartition(":")
    current = f"{volume}:"
    for part in PurePosixPath(rest).parts:
        current = _join(current, part)
        try:
            _with_ctl(lambda: ctl.makedir(current))
        except AmigaError as e:
            if e.code != 202:  # already exists
                raise


@mcp.tool()
def amiga_get(amiga_path: str, local_path: str) -> str:
    """Copy a single file from the Amiga to the local machine."""
    data = _with_ctl(lambda: ctl.read_file(amiga_path))
    target = Path(local_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return f"saved {len(data)} bytes to {target}"


@mcp.tool()
def amiga_mkdir(path: str) -> str:
    """Create a directory, including missing parents."""
    _makedirs(path)
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
    INVENTORY_DIR.mkdir(exist_ok=True)
    scans = sorted(INVENTORY_DIR.glob("*.json"))
    previous = json.loads(scans[-1].read_text()) if scans else None
    target = INVENTORY_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    target.write_text(json.dumps(items, indent=1, ensure_ascii=False))
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
    are screen pixels; use the scale in the reply for reduced images.
    """
    image = _with_screen(lambda: screen.capture(x, y, width or None, height or None))
    scale = max(image.size) / MAX_EDGE
    size = image.size
    if scale > 1:
        image = image.resize((round(size[0] / scale), round(size[1] / scale)))
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    note = (
        f"screen {screen.width}x{screen.height}; region {size[0]}x{size[1]} at ({x},{y}); "
        f"screen_x = {x} + image_x * {max(scale, 1):.3f}, "
        f"screen_y = {y} + image_y * {max(scale, 1):.3f}"
    )
    return [note, McpImage(data=out.getvalue(), format="png")]


@mcp.tool()
def amiga_screenshot_color(x: int, y: int, width: int, height: int) -> list:
    """A region in full colour, for small text or exact colours.

    amiga_screenshot is cheaper and covers the whole screen in 256 colours;
    use this one for the part you need to read precisely.
    """
    image = _with_screen(lambda: screen.capture_rgb(x, y, width, height))
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return [f"region {width}x{height} at ({x},{y}), full colour", McpImage(data=out.getvalue(), format="png")]


@mcp.tool()
def amiga_wait_change(x: int = 0, y: int = 0, width: int = 0, height: int = 0,
                      timeout: float = 30) -> str:
    """Wait until something changes in a region (or anywhere), without sending pictures.

    Use it after a click or command instead of taking screenshots in a loop.
    """
    start = time.monotonic()
    _with_screen(lambda: screen.poll(x, y, width or None, height or None))  # settle first
    while time.monotonic() - start < timeout:
        changed = _with_screen(lambda: screen.poll(x, y, width or None, height or None))
        if changed:
            return f"changed after {time.monotonic() - start:.1f} s ({changed} tiles)"
        time.sleep(0.25)
    return f"no change within {timeout:.0f} s"


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
def amiga_ui_tree() -> str:
    """The front screen's windows and gadgets as text, with labels and boxes.

    Works for standard Intuition and GadTools windows; MUI interiors and games
    still need screenshots. Click entries with amiga_ui_click.
    """
    lines = []
    for item in _with_screen(screen.ui_tree):
        box = f"({item.get('x', 0)},{item.get('y', 0)} {item.get('w', item.get('width', 0))}x" \
              f"{item.get('h', item.get('height', 0))})"
        if item["kind"] == "screen":
            lines.append(f"screen {item['title']!r} {item['width']}x{item['height']}")
        elif item["kind"] == "window":
            lines.append(f"  window {item['id']} {item['title']!r} {box}{' active' if item['active'] else ''}")
        else:
            state = " disabled" if item["disabled"] else ""
            state += " selected" if item["selected"] else ""
            lines.append(f"    {item['type']} {item['label']!r} {box}{state}")
    return "\n".join(lines) or "no windows"


def _centre(item: dict) -> tuple[int, int]:
    return item["x"] + item["w"] // 2, item["y"] + item["h"] // 2


@mcp.tool()
def amiga_ui_click(label: str, window: str = "", button: str = "left", double: bool = False) -> str:
    """Click a gadget by its label (from amiga_ui_tree), optionally in a window by title."""
    tree = _with_screen(screen.ui_tree)
    windows = {w["id"]: w["title"] for w in tree if w["kind"] == "window"}
    gadgets = [g for g in tree if g["kind"] == "gadget"
               and (not window or window.lower() in windows.get(g["window"], "").lower())]
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
    return f"clicked {gadget['label']!r} at ({x},{y})"


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

    The right button opens menus only while held; use amiga_drag for menus.
    """
    _with_screen(lambda: screen.click(x, y, button, double))
    return f"clicked {button} at ({x},{y})"


@mcp.tool()
def amiga_drag(x1: int, y1: int, x2: int, y2: int, button: str = "left") -> str:
    """Press at one point and release at another: drag icons or pick menu items."""
    _with_screen(lambda: screen.drag(x1, y1, x2, y2, button))
    return f"dragged {button} from ({x1},{y1}) to ({x2},{y2})"


@mcp.tool()
def amiga_type(text: str) -> str:
    """Type text into the active window through its keymap. Newline presses Return."""
    _with_screen(lambda: screen.type_text(text))
    return f"typed {len(text)} characters"


@mcp.tool()
def amiga_key(combo: str) -> str:
    """Press a key or combination: 'return', 'esc', 'lamiga+n', 'ctrl+c', 'f1', 'help'."""
    _with_screen(lambda: screen.press(combo))
    return f"pressed {combo}"


viewer: Viewer | None = None


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
    """Restart cleanly through the system and wait until the Amiga answers."""
    with _exclusive():
        ctl.flush(FLUSH_VOLUMES)
        try:
            ctl.exec("Run >NIL: C:Reboot", timeout=5)
        except (OSError, AmigaError):
            pass  # the machine went down before it could answer
        ctl.close()
        with _screen_lock():
            screen.close()
        time.sleep(10)
        if not wait_for_port(HOST, CTL_PORT, 300):
            raise AmigaError("no answer after restart; amiga_power('cycle') may be needed")
    return "Amiga restarted"


@mcp.tool()
def amiga_power(action: str) -> str:
    """Control mains power, if configured: 'on', 'off' or 'cycle'.

    Last resort for a hung Amiga: cutting power loses file changes from the
    last minutes. Prefer amiga_restart while the Amiga still answers.
    """
    if action == "cycle":
        with _exclusive():
            ctl.close()
            with _screen_lock():
                screen.close()
            attempts = power_cycle(HOST, CTL_PORT)
        return f"Amiga is back after {attempts} power cycle(s)"
    set_power(action == "on")
    return f"power {action}"


def main() -> None:
    if not HOST:
        raise SystemExit("set AMIGENT_HOST to the Amiga's address")
    mcp.run()


if __name__ == "__main__":
    main()
