"""MCP server that lets an agent operate a real Amiga."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path, PurePosixPath

from mcp.server.fastmcp import FastMCP, Image as McpImage

from . import aminet, files, install, inventory, locks, machines, notes, others, pack, pictures, screentext
from . import secret, system, uptime, watch
from .client import join_path
from .errors import AmigaError
from .amigentd import Amigentd, AmigentdError
from .channel import Commands, Request, pc_outdated, perform
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


INSTRUCTIONS = """Amigent operates a real Amiga.
- After the Amiga was away, amiga_status first: it tells a restart or a guru,
  and whether another agent (such as amimcp's amiagent) runs there too; then
  ask the user before driving mouse and keys, so the two don't collide.
- To find something out, read amiga_screen_text before taking a screenshot; take
  a picture (best of just the part you need) for layout, graphics and games, or
  when the text says something is missing. After a click, amiga_screenshot
  (changed=True) shows only what changed.
- Wait with amiga_wait_window, amiga_wait_text or amiga_wait_change instead of
  screenshots in a loop; put known sequences into one amiga_do call.
- Long commands go through amiga_job, never through amiga_exec.
- Before using a program again, amiga_notes(topic); afterwards amiga_note what
  would save time next time.
- Before installing software, amiga_install_begin, afterwards amiga_install_end,
  so the user can take it back with amiga_install_undo."""

mcp = FastMCP("amigent", instructions=INSTRUCTIONS)
# One packing policy per Amiga, shared by channel and screen; amigas.json may set "compress".
pack.policy_for(HOST, mode=(machines.find(HOST) or {}).get("compress") if HOST else None)
ctl = Commands(HOST, CTL_PORT, SCREEN_PORT)  # amigentd's channel, or amigactld on older installs
screen = Amigentd(HOST, SCREEN_PORT)

# Every tool call shows up as a line in amigent-monitor's window on the Amiga.
NOTE_HIDDEN = {"key", "password", "token", "secret"}


def note_line(name: str, kwargs: dict, defaults: dict | None = None) -> str:
    """'amiga_start', {'command': 'HiP'} -> 'start HiP'; short, without secrets.

    Arguments left at their default say nothing: the MCP layer fills them in."""
    parts = [name.removeprefix("amiga_")]
    defaults = defaults or {}
    for k, v in kwargs.items():
        if k in NOTE_HIDDEN or v is None or v == "" or v is False:
            continue
        if k in defaults and defaults[k] == v:
            continue
        text = " ".join(str(v).split())
        parts.append(text if len(text) <= 60 else text[:57] + "...")
        if len(" ".join(parts)) > 120:
            break
    return " ".join(parts)


def _note(line: str) -> bool:
    """Best effort and only on a link that is up: the monitor must never cost a tool."""
    backend = getattr(ctl, "backend", None)
    if not getattr(ctl, "connected", False) or not hasattr(backend, "note"):
        return False
    try:
        with _exclusive():
            backend.note(line)
        return True
    except Exception:  # noqa: BLE001 - a monitor line is never worth a failed tool
        return False


class _Exec:
    """ctl.exec through the shared lock, for modules that only take a ctl."""

    def exec(self, command: str, timeout: float = 30):
        return _with_ctl(lambda: ctl.exec(command, timeout=timeout))


# Another agent on the same Amiga: said once per session before input, looked up again
# every few minutes. amiga_status always says it.
INPUT_TOOLS = {"amiga_click", "amiga_type", "amiga_key", "amiga_do", "amiga_start", "amiga_drag",
               "amiga_click_text", "amiga_ui_click", "amiga_menu_select"}
OTHERS_RECHECK = 300.0
_others_seen = {"at": -1e9, "text": "", "told": ""}


def _others_note() -> str:
    """The warning about another running agent, the first time it applies; else ''."""
    if time.monotonic() - _others_seen["at"] > OTHERS_RECHECK:
        try:
            info = _with_ctl(ctl.sysinfo)
            _others_seen["text"] = others.warning(others.from_sysinfo(info))
        except Exception:  # noqa: BLE001 - never worth a failed tool
            pass
        _others_seen["at"] = time.monotonic()
    text = _others_seen["text"]
    if not text or _others_seen["told"] == text:
        return ""
    _others_seen["told"] = text
    return text


_outdated_told = False


def _outdated_note() -> str:
    """Once per session: 'Note: ...' when the Amiga's amigentd is newer than this PC's Amigent."""
    global _outdated_told
    backend = getattr(ctl, "backend", None)  # tests put simple stand-ins in place
    if _outdated_told or backend is None or not getattr(backend, "_sock", None):
        return ""
    _outdated_told = True
    text = pc_outdated(getattr(backend, "protocol", 0))
    return f"Note: {text}\n" if text else ""


def _noted(fn):
    import functools
    import inspect

    defaults = {k: p.default for k, p in inspect.signature(fn).parameters.items()
                if p.default is not inspect.Parameter.empty}

    @functools.wraps(fn)
    def run(*args, **kwargs):
        line = note_line(fn.__name__, kwargs, defaults)
        sent = _note(line)
        try:
            result = fn(*args, **kwargs)
        finally:
            if not sent:  # the tool may have brought the link up
                _note(line)
        if isinstance(result, str):
            result = _outdated_note() + result
        if fn.__name__ in INPUT_TOOLS:
            warn = _others_note()
            if warn and isinstance(result, str):
                result = f"Note: {warn}\n{result}"
            elif warn and isinstance(result, list):
                result = [f"Note: {warn}"] + result
        return result
    return run


_tool = mcp.tool


def _tool_noted(*args, **kwargs):
    register = _tool(*args, **kwargs)
    return lambda fn: register(_noted(fn))


mcp.tool = _tool_noted


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
    ports, tasks = info.pop("ports", None), info.pop("tasks", None)
    report = {"amiga": machines.name_of(HOST), "address": machines.address(HOST)}
    found = others.scan(_Exec(), machines.address(HOST) or HOST,
                        {"ports": ports or "", "tasks": tasks or ""})
    _others_seen.update(at=time.monotonic(), text=others.warning(found))
    if found:
        report["other_agents"] = [f.as_dict() for f in found]
        if _others_seen["text"]:
            report["warning"] = _others_seen["text"]
    if "uptime" in info:
        report["running_for"] = uptime.duration(int(info["uptime"]))
    said = seen.check(machines.name_of(HOST) or machines.address(HOST) or "", info)
    if said:
        report["notes"] = said
    report["system"] = _system_facts()
    report.update({"sysinfo": info, "volumes": _with_ctl(ctl.volumes)})
    outdated = pc_outdated(getattr(getattr(ctl, "backend", None), "protocol", 0))
    if outdated:
        report["update_pc"] = outdated
    if "text" in info:
        report["recording"] = {"screen_text": info.get("text") == "1",
                               "debug_output": info.get("debug") == "1"}
        report["packing"] = pack.policy_for(HOST).describe()
    return json.dumps(report, indent=1)


_system_seen: dict = {}


def _system_facts() -> dict:
    """OS, CPU and PowerPC software, asked once per session; the system doesn't change while it runs."""
    if not _system_seen:
        run = lambda c: (lambda r: (r.rc, r.output))(_with_ctl(lambda: ctl.exec(c)))  # noqa: E731
        facts = system.describe_system(run("Version")[1], run("CPU")[1])
        if system.native_ppc(facts):
            facts["note"] = system.EMULATED_NOTE
        else:
            ppc = system.ppc_software(run)
            if ppc:
                facts["ppc"] = ppc
        _system_seen.update({k: v for k, v in facts.items() if v})
    return dict(_system_seen)


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
windows_at_start: tuple[float, list[int]] = (0.0, [])  # before the last amiga_start
START_WINDOWS = 30.0  # s: wait_window(new) counts windows opened since a start this recent


@mcp.tool()
def amiga_start(command: str) -> str:
    """Start a program in the background, without capturing output.

    Shows what amiga_note recorded about the program earlier, if anything.
    """
    global last_started, windows_at_start
    before = _clear_hold()
    windows_at_start = (time.monotonic(), _window_ids())
    _with_ctl(lambda: ctl.exec(f"Run >NIL: {command}"))
    last_started = command
    known = notes.for_amiga(_amiga()).get(notes.topic_of(command))
    hints = ("\nnotes on it from earlier:\n" + "\n".join(f"- {n}" for n in known)) if known else ""
    return f"{before}started: {command}{hints}"


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
def amiga_screenshot(x: int = 0, y: int = 0, width: int = 0, height: int = 0,
                     changed: bool = False, text: bool = False) -> list:
    """Capture the Amiga screen, or a region of it to read small text.

    Transfers only what changed since the last call, so repeated shots are
    quick. Colours are reduced to 256. All coordinates for the pointer tools
    are screen pixels; use the scale in the reply for reduced images. If the
    same region has not changed since your last shot of it, only a note comes
    back; ask again right away to get the picture anyway. changed=True shows
    only the part that changed since your last look (after a click, say), with
    its place on the screen. text=True adds the recorded text of the region,
    see amiga_screen_text; for information alone that one is far cheaper.
    """
    global last_look, _last_shot
    if changed:
        out = changed_shot()
        return out + ([amiga_screen_text()] if text else [])
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
    out = [note, McpImage(data=pictures.png(image), format="png")]
    if text:
        try:
            out.append(amiga_screen_text(x=x, y=y, width=width, height=height))
        except AmigaError as e:
            out.append(f"no text: {e}")
    return out


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


def _find_screen(screen_id: str) -> dict:
    """A screen by handle from amiga_screens or by part of its title."""
    found = _with_screen(screen.screens)
    try:
        handle = int(screen_id, 0)
        return next(s for s in found if s["handle"] == handle)
    except (ValueError, StopIteration):
        matches = [s for s in found if screen_id.lower() in s["title"].lower()]
        if len(matches) != 1:
            raise AmigaError(f"{len(matches)} screens match {screen_id!r}; use the handle") from None
        return matches[0]


def _screen_handle(screen_id: str) -> int:
    return _find_screen(screen_id)["handle"]


@mcp.tool()
def amiga_screen_front(screen_id: str) -> str:
    """Bring a screen to the front, by handle from amiga_screens or by part of its title."""
    target = _find_screen(screen_id)
    if not _with_screen(lambda: screen.screen_to_front(target["handle"])):
        raise AmigaError("that screen has just closed")
    _with_screen(screen.check_size)
    return f"front screen is now {target['title']!r} ({target['width']}x{target['height']})"


@mcp.tool()
def amiga_mouse_relative(dx: int, dy: int, exact: bool = False) -> str:
    """Move the mouse by a distance, as a real mouse does.

    For games and other programs that read mouse movement and keep their own
    pointer; amiga_click places the Intuition pointer, which those ignore.
    The Amiga's mouse acceleration applies, as to a real mouse. exact=True
    takes dx, dy as screen pixels and moves in small steps, then corrects by
    where the pointer really is, so the pointer lands on the spot. The answer
    says where Intuition has the pointer afterwards.
    """
    def move():
        if not exact:
            screen.move_relative(dx, dy)
            time.sleep(0.05)
            return screen.pointer()
        start = screen.pointer()
        screen.walk_relative(dx, dy)
        return screen.settle_pointer(start[0] + dx, start[1] + dy) if start else None

    at = _with_screen(move)
    where = f"; pointer now at ({at[0]},{at[1]})" if at else ""
    return f"moved by ({dx},{dy}){where}"


@mcp.tool()
def amiga_ui_tree(pictures: bool = True, screen_id: str = "") -> list:
    """The front screen's windows and gadgets as text, with labels, boxes and numbers.

    screen_id (a handle from amiga_screens or part of a title) looks at a screen
    behind the front one without bringing it forward; pictures only come from
    the front screen.

    Labels come from classic Intuition gadgets and GadTools checkboxes, strings
    and sliders. Gadgets whose label can't be read (GadTools buttons, BOOPSI,
    ReAction, MUI, requesters patched by tools like ReqAttack) come with a
    picture: each one cut out of the screen next to its #number, so you can
    read what it says and click it with amiga_ui_click(number=..., window=...).
    Only the active window's gadgets get pictures.
    """
    handle = _screen_handle(screen_id) if screen_id else 0
    tree = _with_screen(lambda: screen.ui_tree(handle) if handle else screen.ui_tree())
    if handle:
        got = next((i.get("handle") for i in tree if i["kind"] == "screen"), None)
        if got != handle:
            raise AmigaError("that screen has just closed, or amigentd is older than 1.3.0 "
                             "and only reads the front screen")
        pictures = pictures and _with_screen(screen.screens)[0]["handle"] == handle
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
    if len(matches) > 1 and window and window not in windows:
        # several windows share the title: the frontmost one (first in the tree) counts
        first = matches[0]["window"]
        matches = [g for g in matches if g["window"] == first]
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
    Buttons arrive as input events, as a mouse's do for Intuition and every
    program that reads input. Programs that read the button straight from the
    hardware (left: CIA-A $BFE001 bit 6; right/middle: POTGO), such as
    ProTracker, many games and demos, don't see them: amigentd doesn't drive
    those port pins, as that could fight a real mouse's button. Use keys
    there, or a real mouse.
    """
    if button not in ("left", "right", "middle"):
        raise AmigaError("button must be left, right or middle")
    _with_screen(lambda: screen.button(button, down))
    return f"{button} button {'down' if down else 'up'}"


@mcp.tool()
def amiga_drag(x1: int, y1: int, x2: int, y2: int, button: str = "left",
               relative: bool | None = None) -> str:
    """Press at one point and release at another: drag icons, move and size
    windows by their drag bar and size gadget, or pick menu items.

    By default the way goes as mouse movement in small steps with the button
    held, as a hand does: window drag bars and size gadgets follow only that.
    relative=True uses nothing else, for programs that follow the mouse
    themselves and ignore where the pointer is put (ProTracker 4's window, many
    games); relative=False puts the pointer at the end in one jump.
    """
    _with_screen(lambda: screen.drag(x1, y1, x2, y2, button, relative))
    how = {True: " by mouse movement only", False: " in one jump"}.get(relative, "")
    return f"dragged {button} from ({x1},{y1}) to ({x2},{y2})" + how


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


# ---- 1.3.0: text instead of pictures, steps in one call, waiting on the Amiga

def _amiga() -> str:
    return machines.name_of(HOST) or machines.address(HOST) or "Amiga"


def _screen_text() -> screentext.ScreenText:
    try:
        lines = _with_ctl(ctl.screen_text)
    except AttributeError:
        raise AmigaError("reading the screen's text needs amigentd 1.3.0; run amigent-setup "
                         "--update") from None
    return screentext.parse(lines)


@mcp.tool()
def amiga_screen_text(window: str = "", find: str = "", x: int = 0, y: int = 0, width: int = 0,
                      height: int = 0, hidden: bool = False) -> str:
    """The text on the front screen, window by window, with positions: cheap, no picture.

    Use it first when you need information: Shell output, a requester's
    message, a list, a status line, whether something finished. It costs a
    few hundred bytes where a screenshot costs kilobytes. Take amiga_screenshot
    instead (or afterwards) for layout, graphics, icons, colours, games, and
    whenever this reports no text or a note says text is missing: buttons drawn
    as pictures, windows that were open before amigentd started, or programs
    that draw text their own way. window picks a window by part of its title
    or its id; find keeps rows containing that text; x/y/width/height keep rows
    in that region. Rows covered by a window in front are left out unless
    hidden=True. Positions are screen pixels, ready for amiga_click.
    """
    region = (x, y, width, height) if (x or y or width or height) else None
    return screentext.render(_screen_text(), window, find, region, hidden)


@mcp.tool()
def amiga_click_text(text: str, window: str = "", button: str = "left", double: bool = False) -> str:
    """Click on visible text, such as a button label or a list entry, found via the screen text.

    Works for buttons amiga_ui_tree can't name (MUI, ReAction, requesters)
    as long as their label is drawn as text. Exact matches win over rows that
    merely contain the text.
    """
    st = _screen_text()
    hit = screentext.find_row(st, text, window)
    if not hit:
        raise AmigaError(f"{text!r} is not in the visible text; amiga_screen_text shows what is, "
                         "amiga_screenshot what is drawn as pictures")
    win, row = hit
    px, py = screentext.point_of(row, text)
    _with_screen(lambda: screen.click(px, py, button, double))
    return f"clicked {text!r} in {win.title!r} at ({px},{py})"


WAIT_KINDS = {"window": 1, "window_gone": 2, "text": 3, "text_gone": 4}


def _window_ids() -> list[int]:
    try:
        return [int(w["id"], 16) for w in _with_screen(screen.ui_tree) if w["kind"] == "window"]
    except (AmigaError, AmigentdError, OSError, ValueError, KeyError, AttributeError):
        return []


def _wait(kind: str, what: str, timeout: float, new: bool = False) -> str:
    code = WAIT_KINDS[kind] | (8 if new else 0)
    # Right after amiga_start, a window may already be open by the time the wait
    # begins: then "new" means not open before the start.
    since, ids = windows_at_start
    known = ids if new and since and time.monotonic() - since < START_WINDOWS else None
    try:
        lines = _with_ctl(lambda: ctl.wait_for(code, what, timeout, known))
    except AttributeError:
        raise AmigaError("waiting on the Amiga needs amigentd 1.3.0; run amigent-setup --update") from None
    except AmigaError as e:
        if e.code == 2:
            return f"timeout: {'no ' if not kind.endswith('gone') else ''}{kind.split('_')[0]} " \
                   f"{what!r} {'still there ' if kind.endswith('gone') else ''}after {timeout:g} s"
        raise
    if kind.endswith("gone"):
        return f"{kind.split('_')[0]} {what!r} is gone"
    f = lines[0].split("\t") if lines else []
    if kind == "window" and len(f) >= 8:
        return (f"window {f[7]!r} {f[1]} ({f[2]},{f[3]} {f[4]}x{f[5]})"
                f"{' active' if 'a' in f[6] else ''}{' on a screen behind' if 'h' in f[6] else ''}")
    if kind == "text" and len(f) >= 7:
        return f"text {f[6]!r} at ({f[2]},{f[3]} {f[4]}x{f[5]}) in window {f[1]}"
    return "\n".join(lines)


@mcp.tool()
def amiga_wait_window(title: str, timeout: float = 30, new: bool = False, gone: bool = False) -> str:
    """Wait on the Amiga itself until a window with this title opens (or closes, gone=True).

    title is part of the title, or an AmigaDOS pattern. With new=True only a
    window that was not open yet counts, e.g. a second requester; right after
    amiga_start, one that was not open before the start. The Amiga
    looks ten times a second and the line stays quiet meanwhile, so this is
    the cheap way to wait for a program, a requester or the end of a dialog.
    """
    return _wait("window_gone" if gone else "window", title, timeout, new)


@mcp.tool()
def amiga_wait_text(text: str, timeout: float = 30, gone: bool = False) -> str:
    """Wait on the Amiga until this text shows up on the front screen (or is gone).

    For "done", a prompt coming back, a progress line. Any case; it looks at
    the text amigentd records, see amiga_screen_text.
    """
    return _wait("text_gone" if gone else "text", text, timeout)


def _changed_boxes(since: float, most: int = 4) -> list[tuple[int, int, int, int]]:
    """The areas of tiles that came in after since: neighbouring tiles form one box,
    separate places stay separate, largest first."""
    tiles = {(tx, ty): (tw, th) for t, tx, ty, tw, th in list(screen.changes) if t > since}
    boxes, seen = [], set()
    for start in tiles:
        if start in seen:
            continue
        group, todo = [], [start]
        seen.add(start)
        while todo:  # tiles touching (also corner to corner) belong together
            x, y = todo.pop()
            group.append((x, y))
            for dx in (-64, 0, 64):
                for dy in (-64, 0, 64):
                    n = (x + dx, y + dy)
                    if n in tiles and n not in seen:
                        seen.add(n)
                        todo.append(n)
        x0 = min(x for x, _ in group)
        y0 = min(y for _, y in group)
        x1 = max(x + tiles[(x, y)][0] for x, y in group)
        y1 = max(y + tiles[(x, y)][1] for x, y in group)
        boxes.append((x0, y0, x1 - x0, y1 - y0))
    boxes.sort(key=lambda b: -b[2] * b[3])
    return boxes[:most]


def _changed_box(since: float) -> tuple[int, int, int, int] | None:
    tiles = [(tx, ty, tw, th) for t, tx, ty, tw, th in list(screen.changes) if t > since]
    if not tiles:
        return None
    x0 = min(t[0] for t in tiles)
    y0 = min(t[1] for t in tiles)
    x1 = max(t[0] + t[2] for t in tiles)
    y1 = max(t[1] + t[3] for t in tiles)
    return x0, y0, x1 - x0, y1 - y0


def changed_shot() -> list:
    """Only the parts of the screen that changed since the last look, with their place."""
    global last_look
    if paused := _paused_note():
        return [paused]
    since = last_look
    _with_screen(lambda: screen.capture())  # brings in every changed tile
    last_look = time.monotonic()
    every = [(0, 0, screen.width, screen.height)]
    boxes = (_changed_boxes(since) if since else every) or []
    if not boxes:
        return ["no change on the screen since your last look"]
    total = len(_changed_boxes(since, 1000)) if since else 1
    out = []
    for x, y, w, h in boxes:
        image = _with_screen(lambda: screen.capture(x, y, w, h))
        image, scale = pictures.fit(image)
        out.append(f"changed: {w}x{h} at ({x},{y}) of {screen.width}x{screen.height}; "
                   f"screen_x = {x} + image_x * {scale:.3f}, screen_y = {y} + image_y * {scale:.3f}")
        out.append(McpImage(data=pictures.png(image), format="png"))
    if total > len(boxes):
        out.append(f"note: {total - len(boxes)} smaller changed places left out")
    return out


STEP_KEYS = ("click", "click_text", "ui_click", "menu", "key", "type", "move", "drag", "wait",
             "wait_change", "wait_window", "wait_text", "start", "exec", "screen_front")


def _step(step: dict) -> str:
    kind = next((k for k in STEP_KEYS if k in step), None)
    if not kind:
        raise AmigaError(f"a step needs one of {', '.join(STEP_KEYS)}")
    v = step[kind]
    timeout = float(step.get("timeout", 10))
    if kind == "click":
        return amiga_click(int(v[0]), int(v[1]), step.get("button", "left"), bool(step.get("double")))
    if kind == "click_text":
        return amiga_click_text(str(v), step.get("window", ""), step.get("button", "left"),
                                bool(step.get("double")))
    if kind == "ui_click":
        return amiga_ui_click(str(v) if not isinstance(v, int) else "", step.get("window", ""),
                              v if isinstance(v, int) else int(step.get("number", -1)),
                              step.get("button", "left"), bool(step.get("double")))
    if kind == "menu":
        return amiga_menu_select(str(v))
    if kind == "key":
        return amiga_key(str(v))
    if kind == "type":
        return amiga_type(str(v), float(step.get("settle", 10)))
    if kind == "move":
        return amiga_mouse_move(int(v[0]), int(v[1]))
    if kind == "drag":
        return amiga_drag(*(int(n) for n in v[:4]), step.get("button", "left"))
    if kind == "wait":
        time.sleep(min(float(v), 60))
        return f"waited {float(v):g} s"
    if kind == "wait_change":
        box = list(v) + [0] * (4 - len(v)) if isinstance(v, list) else [0, 0, 0, 0]
        return amiga_wait_change(*box[:4], timeout=timeout)
    if kind == "wait_window":
        return amiga_wait_window(str(v), timeout, bool(step.get("new")), bool(step.get("gone")))
    if kind == "wait_text":
        return amiga_wait_text(str(v), timeout, bool(step.get("gone")))
    if kind == "start":
        return amiga_start(str(v))
    if kind == "exec":
        return amiga_exec(str(v), step.get("cd", ""), float(step.get("timeout", 30)))
    return amiga_screen_front(str(v))


@mcp.tool()
def amiga_do(steps: list[dict], look: str = "none") -> list:
    """Several actions in one call, then one look: saves a round trip per action.

    steps is a list of objects with one action each, carried out in order:
    {"click": [x, y]} (with "button", "double"), {"click_text": "OK"} (with
    "window"), {"ui_click": "Save"} or {"ui_click": 3, "window": "Prefs"},
    {"menu": "Project/Quit"}, {"key": "return"}, {"type": "dir\\n"},
    {"move": [x, y]}, {"drag": [x1, y1, x2, y2]}, {"wait": 1.5},
    {"wait_change": [x, y, w, h], "timeout": 10}, {"wait_window": "Title",
    "timeout": 10, "new": true}, {"wait_text": "done", "timeout": 20},
    {"start": "SYS:Utilities/Clock"}, {"exec": "Info"}, {"screen_front": "Workbench"}.
    It stops at the first step that fails and says which. look is what comes
    back at the end: none, text (amiga_screen_text), changed (only the part of
    the screen that changed) or screen (a screenshot).
    """
    if not isinstance(steps, list) or not steps:
        raise AmigaError("steps is a list of actions, e.g. [{\"click\": [10, 20]}]")
    if len(steps) > 50:
        raise AmigaError("at most 50 steps in one call")
    log = []
    for i, step in enumerate(steps, 1):
        try:
            result = _step(step if isinstance(step, dict) else {})
        except (AmigaError, OSError, ValueError, TypeError, IndexError) as e:
            log.append(f"step {i} failed: {e}")
            left = len(steps) - i
            log.append("stopped" if not left else f"stopped; step {i + 1} not done" if left == 1
                       else f"stopped; steps {i + 1} to {len(steps)} not done")
            break
        text = result if isinstance(result, str) else " ".join(r for r in result if isinstance(r, str))
        log.append(f"{i}. {text}")
    out: list = ["\n".join(log)]
    look = look.strip().lower()
    if look == "text":
        out.append(amiga_screen_text())
    elif look == "changed":
        out += changed_shot()
    elif look in ("screen", "screenshot"):
        out += amiga_screenshot()
    elif look not in ("", "none"):
        out.append(f"note: look {look!r} unknown; none, text, changed or screen")
    return out


# ---- packing

@mcp.tool()
def amiga_link(compress: str = "") -> str:
    """How data travel to and from the Amiga, and whether they are packed.

    Packing pays when the Amiga packs much faster than its network carries,
    e.g. a PiStorm or 68080 behind a plipbox; a slow CPU on a fast network
    stays unpacked. compress sets it for this Amiga from now on: auto (decides
    by measurement, the default), off, fast or best.
    """
    policy = pack.policy_for(HOST)
    if compress:
        try:
            policy.set_mode(compress)
        except ValueError as e:
            raise AmigaError(str(e)) from None
    if not policy.known:
        try:
            _with_ctl(ctl.sysinfo)  # a channel learns the Amiga's rates on connect
        except AmigaError:
            pass
    return json.dumps(policy.describe(), indent=1)


# ---- developers

debug_seen = 0  # RawPutChar count at the last look


@mcp.tool()
def amiga_debug_log(everything: bool = False) -> str:
    """Debug output on the Amiga since the last call: kprintf, Enforcer and MuForce hits.

    amigentd catches what goes through exec's RawPutChar (as Sashimi does);
    the serial port still gets it as well. everything=True shows the whole
    ring (64 KB) instead of what is new.
    """
    global debug_seen
    try:
        total, data = _with_ctl(lambda: ctl.debug_log(0 if everything else debug_seen))
    except AttributeError:
        raise AmigaError("debug output needs amigentd 1.3.0; run amigent-setup --update") from None
    debug_seen = total
    text = data.decode("iso-8859-1").replace("\r\n", "\n").replace("\r", "\n")
    return text if text else "no new debug output"


followers: dict = {}  # source -> follow.Follower


@mcp.tool()
def amiga_follow(source: str = "debug", local_path: str = "", stop: bool = False,
                 every: float = 1.0) -> str:
    """Copy a growing log from the Amiga to this PC while it grows, so it survives a crash.

    source is "debug" (kprintf, Enforcer and MuForce output) or a file on the
    Amiga, such as a program's log. Every `every` seconds what is new goes to
    local_path (default: amigent's config folder, follow/<name>.log), on a
    connection of its own. When the Amiga hangs, the copy holds everything up to
    that moment, also what its filesystem had not yet written to disk, with a
    marked line where the Amiga stopped answering. stop=True ends one (or all,
    with source ""); without arguments while followers run it lists them.
    """
    from . import follow, paths

    if stop:
        names = [source] if source else list(followers)
        done = []
        for name in names:
            f = followers.pop(name, None)
            if f:
                f.stop()
                done.append(f"{name}: {f.written} bytes in {f.target}")
        return "stopped " + "; ".join(done) if done else "nothing was being followed"
    if source in followers:
        f = followers[source]
        state = f" (Amiga not answering since {time.strftime('%H:%M:%S', time.localtime(f.lost_since))})" \
            if f.lost_since else ""
        return f"already following {source}: {f.written} bytes in {f.target}{state}"
    own = Commands(HOST, CTL_PORT, SCREEN_PORT, timeout=15)
    if source == follow.DEBUG:
        try:
            start, _ = own.debug_log(0xFFFFFFFF)  # only the count; the old output stays there
        except AttributeError:
            raise AmigaError("debug output needs amigentd 1.3.0; run amigent-setup --update") from None
        fetch = follow.debug_fetch(own.debug_log)
    else:
        start = int(own.stat(source).get("size", 0) or 0)
        fetch = follow.file_fetch(own.read_from, own.stat, source)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", source.replace(":", "_")).strip("_") or "log"
    target = Path(local_path).expanduser() if local_path else paths.FOLDER / "follow" / f"{name}.log"
    followers[source] = follow.Follower(source, target, fetch, max(0.2, every), start).start()
    return (f"following {source} into {target}, from now on (what is already there: "
            f"amiga_get or amiga_debug_log)")


RUN_DIR = "RAM:amigent-run"
CRASH_TITLES = ("software failure", "softwarefehler", "program failed", "programm fehlgeschlagen",
                "guru", "systemanforderung", "system request")


@mcp.tool()
def amiga_run(program: str, args: str = "", timeout: float = 60, cd: str = "") -> str:
    """Develop and test: copy a program over if it is local, run it, and report everything.

    program is a path on this PC (uploaded to RAM:amigent-run first, only when
    it changed) or on the Amiga. Reports return code and output, the debug
    output it produced (kprintf, Enforcer hits), requesters that came up
    meanwhile (such as Software Failure) and, if the Amiga dropped off, what
    amiga_status finds after it came back: a restart or a guru code.
    """
    global debug_seen
    local = Path(program).expanduser()
    if local.is_file():
        _with_ctl(lambda: files.makedirs(ctl, RUN_DIR))
        target = _join(RUN_DIR, local.name)
        _with_ctl(lambda: files.put(ctl, local, target))
        _with_ctl(lambda: ctl.exec(f'Protect "{target}" +rwed'))
        command = f'"{target}" {args}'.strip()
    else:
        command = f"{program} {args}".strip()
    try:
        seen_before, _ = _with_ctl(lambda: ctl.debug_log(0xFFFFFFFF))
    except (AttributeError, AmigaError):
        seen_before = None
    windows_before = _window_titles()
    report = []
    try:
        result = _with_ctl(lambda: ctl.exec(command, cd or None, timeout))
        report.append(f"rc={result.rc}")
        if result.output.strip():
            report.append(f"output:\n{result.output.rstrip()}")
    except AmigaError as e:
        report.append(f"did not finish: {e}")
        if e.code is None:  # the link is gone: a crash?
            time.sleep(5)
            try:
                report.append(f"after reconnecting:\n{amiga_status()}")
            except AmigaError as down:
                report.append(f"the Amiga does not answer ({down}); amiga_status once it is back")
            return "\n".join(report)
    if seen_before is not None:
        try:
            total, data = _with_ctl(lambda: ctl.debug_log(seen_before))
            debug_seen = total
            if data:
                report.append("debug output:\n" + data.decode("iso-8859-1").rstrip())
        except AmigaError:
            pass
    new = [t for t in _window_titles() if t not in windows_before]
    if new:
        report.append("new windows: " + ", ".join(repr(t) for t in new))
        if any(any(c in t.lower() for c in CRASH_TITLES) for t in new):
            report.append("one looks like a crash requester: amiga_screen_text or a screenshot "
                          "shows it; answer it before running again")
    return "\n".join(report)


def _window_titles() -> list[str]:
    try:
        return [w["title"] for w in _with_screen(screen.ui_tree) if w["kind"] == "window"]
    except (AmigaError, OSError):
        return []


# ---- clipboard

@mcp.tool()
def amiga_clipboard(text: str | None = None) -> str:
    """Read the Amiga's clipboard, or set it to text (then RAmiga+V pastes it there)."""
    from .viewer import CLIPBOARD, MAX_CLIP, ftxt, ftxt_text
    if text is not None:
        _with_ctl(lambda: ctl.write_file(CLIPBOARD, ftxt(text)))
        return f"clipboard set ({len(text)} characters)"
    try:
        size = int(_with_ctl(lambda: ctl.stat(CLIPBOARD)).get("size", 0) or 0)
    except AmigaError as e:
        if e.code is None:
            raise
        return "the clipboard is empty"
    if size > MAX_CLIP:
        return "the clipboard holds more than text (a picture, say)"
    return ftxt_text(_with_ctl(lambda: ctl.read_file(CLIPBOARD))) or "the clipboard holds no text"


# ---- notes

@mcp.tool()
def amiga_notes(topic: str = "", search: str = "") -> str:
    """What was learned earlier about programs on this Amiga: read before using one again.

    Without arguments the topics; with topic its notes; search looks through
    all of them. amiga_start shows the notes of the program it starts.
    """
    book = notes.for_amiga(_amiga())
    if topic:
        found = book.get(topic)
        return "\n".join(f"- {n}" for n in found) if found else f"no notes on {topic!r}"
    if search:
        hits = book.search(search)
        return "\n".join(f"{t}:\n" + "\n".join(f"- {n}" for n in v) for t, v in hits.items()) \
            or f"no notes mention {search!r}"
    topics = book.topics()
    return "\n".join(f"{t} ({n})" for t, n in topics.items()) or "no notes yet"


@mcp.tool()
def amiga_note(topic: str, text: str = "", remove: str = "") -> str:
    """Remember something for next time, for a program or task on this Amiga.

    Write notes that save time later: the stack a game needs, where a button
    sits, which requester comes up and what to answer, a working command line.
    topic is the program's name (e.g. 'settlers2'). remove drops the topic's
    notes containing that text ('*' for all of them).
    """
    book = notes.for_amiga(_amiga())
    if remove:
        gone = book.remove(topic, "" if remove == "*" else remove)
        return f"removed {gone} note(s) from {topic!r}"
    try:
        count = book.add(topic, text)
    except ValueError as e:
        raise AmigaError(str(e)) from None
    return f"noted; {topic!r} has {count} note(s)"


# ---- installations that can be undone

def _home() -> str:
    return AMIGA_DIR


@mcp.tool()
def amiga_install_begin(name: str, folders: str = "", system: bool = True) -> str:
    """Before installing something: take stock so it can be undone, and copy the system aside.

    Records C:, LIBS:, DEVS:, S:, L:, ENVARC:, SYS:WBStartup and SYS:Classes,
    plus folders (comma separated, e.g. the program's target folder), and copies
    the existing ones aside on the Amiga. system=False watches only folders,
    for a game that stays in its own drawer. Then install as usual and call
    amiga_install_end(name).
    """
    name = install.name_ok(name)
    extra = [f.strip() for f in folders.split(",") if f.strip()]
    base = list(install.SYSTEM) if system else []
    if not base and not extra:
        raise AmigaError("with system=False, name the folders to watch")
    paths = _with_ctl(lambda: install.expand(ctl, base + [f for f in extra if f not in base]))
    record = install.Record(name, _home(), paths, started=time.strftime("%Y-%m-%d %H:%M:%S"))
    for path in record.paths:
        snap = _with_ctl(lambda: install.snapshot(ctl, path))
        if snap is None:
            record.missing.append(path)
        record.before[path] = snap or {}
    script = install.begin_script(record)
    _with_ctl(lambda: ctl.write_file("RAM:amigent-install.script", script.encode("iso-8859-1")))
    result = _with_ctl(lambda: ctl.exec("Execute RAM:amigent-install.script", timeout=3600))
    if result.rc:
        raise AmigaError(f"copying the system aside failed (rc={result.rc}): {result.output[-500:]}")
    path = install.save(_amiga(), record)
    files_count = sum(len(v) for v in record.before.values())
    return (f"ready: {files_count} entries in {len(record.paths)} folders recorded and copied to "
            f"{record.store}; install now, then amiga_install_end({name!r}). Record: {path}")


@mcp.tool()
def amiga_install_end(name: str) -> str:
    """After installing: what came, changed and went; keeps what undo needs, writes a repair module."""
    record = install.load(_amiga(), name)
    if record.done:
        return f"{name!r} was finished already; amiga_install_undo({name!r}) takes it back"
    kinds: dict[str, dict] = {}  # full path -> entry as it is after the installation
    for path in record.paths:
        after = _with_ctl(lambda: install.snapshot(ctl, path))
        added, changed, removed = install.compare(record.before.get(path), after)
        for rel in added + changed:
            kinds[install.join(path, rel)] = (after or {})[rel]
        record.added += [install.join(path, r) for r in added]
        record.changed += [install.join(path, r) for r in changed]
        record.removed += [install.join(path, r) for r in removed]
    keeps = record.added or record.changed
    for script, name_ in ((install.prune_script(record), "prune"),
                          (install.apps_script(record, kinds) if keeps else "", "apps")):
        if not script:
            continue
        _with_ctl(lambda: ctl.write_file(f"RAM:amigent-install-{name_}.script", script.encode("iso-8859-1")))
        _with_ctl(lambda: ctl.exec(f"Execute RAM:amigent-install-{name_}.script", timeout=3600))
    notes_ = []
    if keeps:
        module = install.repair_module(record, kinds)
        target = install.join(install.join(record.home, "repair.d"), f"{record.name}.repair")
        _with_ctl(lambda: files.makedirs(ctl, install.join(record.home, "repair.d")))
        _with_ctl(lambda: ctl.write_file(target, module.encode("iso-8859-1")))
        notes_.append(f"repair module {target}")
    record.done = True
    path = install.save(_amiga(), record)
    _with_ctl(lambda: ctl.write_file(install.join(record.store, "record.json"),
                                     record.to_json().encode("utf-8", "replace")))

    def some(items):
        return ", ".join(items[:12]) + (f" and {len(items) - 12} more" if len(items) > 12 else "")
    lines = [f"{name!r}: {len(record.added)} added, {len(record.changed)} changed, "
             f"{len(record.removed)} removed"]
    for label, items in (("added", record.added), ("changed", record.changed), ("removed", record.removed)):
        if items:
            lines.append(f"{label}: {some(items)}")
    lines += notes_ + [f"amiga_install_undo({name!r}) takes it back. Record: {path}"]
    return "\n".join(lines)


@mcp.tool()
def amiga_install_undo(name: str) -> str:
    """Take an installation back: remove what it added, restore what it changed or removed."""
    record = install.load(_amiga(), name)
    if not record.done:
        raise AmigaError(f"{name!r} was not finished: call amiga_install_end({name!r}) first")
    if record.undone:
        return f"{name!r} was taken back already"
    dirs = []
    for path in record.paths:
        before = record.before.get(path, {})
        for full in record.added:
            prefix = install.join(path, "")
            if full.startswith(prefix):
                rel = full[len(prefix):]
                if rel not in before:
                    snap_type = None
                    try:
                        snap_type = _with_ctl(lambda: ctl.stat(full)).get("type")
                    except AmigaError:
                        pass
                    if snap_type == "dir":
                        dirs.append(full)
    script = install.undo_script(record, dirs)
    _with_ctl(lambda: ctl.write_file("RAM:amigent-undo.script", script.encode("iso-8859-1")))
    result = _with_ctl(lambda: ctl.exec("Execute RAM:amigent-undo.script", timeout=3600))
    left = []
    for path in record.paths:
        after = _with_ctl(lambda: install.snapshot(ctl, path))
        added, changed, removed = install.compare(record.before.get(path), after)
        left += [install.join(path, r) for r in added + changed + removed]
    module = install.join(install.join(record.home, "repair.d"), f"{record.name}.repair")
    try:
        _with_ctl(lambda: ctl.delete(module))
    except AmigaError:
        pass
    record.undone = not left
    install.save(_amiga(), record)
    if left:
        return (f"{name!r} taken back except {len(left)} entries (rc={result.rc}): "
                + ", ".join(left[:12]))
    return f"{name!r} taken back: the recorded folders are as before the installation"


@mcp.tool()
def amiga_installs() -> str:
    """Installations recorded on this Amiga and whether they can still be undone."""
    rows = []
    for r in install.listing(_amiga()):
        state = "undone" if r.undone else "finished" if r.done else "begun, not finished"
        rows.append(f"{r.name}: {state}, {len(r.added)} added, {len(r.changed)} changed, "
                    f"{len(r.removed)} removed, since {r.started}")
    return "\n".join(rows) or "no installations recorded"


# ---- Aminet

@mcp.tool()
def amiga_aminet_search(query: str, limit: int = 15) -> str:
    """Search Aminet from the PC: archive path, size, date, downloads, architecture, description."""
    try:
        found = aminet.search(query, max(1, min(limit, 50)))
    except OSError as e:
        raise AmigaError(f"Aminet does not answer: {e}") from e
    if not found:
        return f"nothing on Aminet for {query!r}"
    return "\n".join(f"{p.path}  {p.size}  {p.date}  {p.downloads} dl  {p.arch}  {p.description}"
                     for p in found)


@mcp.tool()
def amiga_aminet_readme(path: str) -> str:
    """The readme of an Aminet archive, e.g. 'util/arc/lha.run': requirements, version, notes."""
    try:
        return aminet.readme(path)
    except (OSError, ValueError) as e:
        raise AmigaError(str(e)) from e


def _member_parts(member: str) -> list[str]:
    """The path parts of an archive member that can't leave the folder it is unpacked into:
    both slashes split, no . or .., nothing with a drive letter or an Amiga volume."""
    parts = [p for p in re.split(r"[/\\]", member) if p not in ("", ".", "..")]
    return [] if any(":" in p for p in parts) else parts


@mcp.tool()
def amiga_aminet_install(path: str, target: str) -> str:
    """Fetch an Aminet archive on the PC and unpack it on the Amiga into target.

    path as amiga_aminet_search shows it, e.g. 'game/strat/foo.lha'; target a
    folder such as 'SYS:Games'. The archive travels packed and the Amiga's own
    lha, unlzx or unzip unpacks it; without one, the PC unpacks .lha and .zip
    and sends the files. Disk images (.adf, .adz) are read on the PC and their
    files sent; .dms is not supported. Use amiga_install_begin first to make it undoable,
    and read the readme for an installer to run afterwards.
    """
    import io
    import tempfile
    import zipfile
    from . import lzh

    try:
        data = aminet.download(path)
    except (OSError, ValueError) as e:
        raise AmigaError(f"download failed: {e}") from e
    name = PurePosixPath(aminet.check_path(path)).name
    tool = aminet.tool_for(name)
    _with_ctl(lambda: files.makedirs(ctl, target))
    has_tool = False
    if tool:
        found = _with_ctl(lambda: ctl.exec(f"Which {tool}", timeout=15))
        has_tool = found.rc == 0
    if has_tool:
        archive = _join("T:", name)
        _with_ctl(lambda: ctl.write_file(archive, data))
        result = _with_ctl(lambda: ctl.exec(aminet.unpack_line(tool, archive, target), timeout=900))
        _with_ctl(lambda: ctl.delete(archive))
        if result.rc >= 10:
            raise AmigaError(f"{tool} failed (rc={result.rc}): {result.output[-800:]}")
        how = f"unpacked on the Amiga with {tool}"
    else:
        if name.lower().endswith((".lha", ".lzh")):
            members = lzh.extract_all(data)
        elif name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                members = {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}
        elif name.lower().endswith((".adf", ".adz")):
            from . import adf
            try:
                members = adf.extract_all(data)
            except adf.AdfError as e:
                raise AmigaError(f"{name}: {e}") from e
        elif name.lower().endswith(".dms"):
            raise AmigaError(f"{name} is a DMS disk archive: Amigent can't unpack those. Unpack "
                             "it with xDMS on the Amiga, or fetch an .adf version")
        else:
            raise AmigaError(f"the Amiga has no {tool or 'unpacker'} for {name}, and the PC can't "
                             "unpack it either; install that tool first")
        with tempfile.TemporaryDirectory() as tmp:
            for member, body in members.items():
                parts = _member_parts(member)
                if not parts:
                    continue
                local = Path(tmp, *parts)
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(body)
            _with_ctl(lambda: files.put(ctl, Path(tmp), target))
        how = f"unpacked on the PC ({len(members)} files) and sent"
    top = _with_ctl(lambda: ctl.list_dir(target))
    return (f"{name} ({len(data) // 1024} KB) {how} into {target}. {target} now holds: "
            + ", ".join(e["name"] + ("/" if e["type"] == "DIR" else "") for e in top[:30]))


def main() -> None:
    if not HOST:
        raise SystemExit("no Amiga known yet: run amigent-setup, or set AMIGENT_HOST")
    mcp.run()


if __name__ == "__main__":
    main()
