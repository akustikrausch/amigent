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


@mcp.tool()
def amiga_start(command: str) -> str:
    """Start a program in the background, without capturing output."""
    _with_ctl(lambda: ctl.exec(f"Run >NIL: {command}"))
    return f"started: {command}"


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
