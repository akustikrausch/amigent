"""MCP server that lets an agent operate a real Amiga."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import re
import time
from pathlib import Path, PurePosixPath

from mcp.server.fastmcp import FastMCP, Image as McpImage

from . import aminet, files, install, inventory, locks, machines, notes, others, pack, pictures, screentext
from . import __version__
from . import gdb, journal, memory, net, probes, secret, system, update, uptime, watch
from . import emu68net, serial_log
from .sessions import Sessions, active, bound, bind_callback, current as _session
from .client import join_path
from .errors import AmigaError
from .amigentd import Amigentd, AmigentdError
from .channel import Commands, Request, pc_outdated, perform
from .power import BOOT_WAIT, RESTART_WAIT, power_cycle, set_power, wait_for_port, flush_volumes, reboot, wait_gone
from .power import available as power_available
from .viewer import Viewer, open_window

CTL_PORT = int(os.environ.get("AMIGENT_CTL_PORT", "6800"))
SCREEN_PORT = int(os.environ.get("AMIGENT_SCREEN_PORT", "6801"))
AMIGA_DIR = os.environ.get("AMIGENT_AMIGA_DIR", "SYS:amigent")
MAX_EDGE = pictures.MAX_EDGE  # larger images get downscaled by the model API anyway
SAME_SHOT = 120.0  # seconds an unchanged region counts as already seen
VIEWER_PORT = int(os.environ.get("AMIGENT_VIEWER_PORT", "6810"))
FLUSH_VOLUMES = flush_volumes()
SLOW_COMMAND = re.compile(r"^\s*(list|search|dir)\b.*\bALL\b", re.IGNORECASE)

def inventory_folder() -> Path:
    """One shelf per Amiga for its scans, next to keys and certificates."""
    return secret.FOLDER / "inventory" / secret.safe(machines.identity(_session().host) or "Amiga")


INSTRUCTIONS = """Amigent operates registered Amigas.
- Use amiga_machines to list saved names, IDs, addresses and reachability.
- An omitted machine uses AMIGENT_HOST, the saved default, or the sole Amiga.
  For requests involving several machines, pass machine explicitly and repeat the workflow
  for each named machine, keeping installation records and results separate.
- A failure on one machine does not imply success or failure on another.
- Aminet search and readme tools are shared; check compatibility for each
  target and wrap each install with install_begin and install_end.
- After the Amiga was away, amiga_status first: it tells a restart or a guru,
  and whether another agent's bridge runs there too; then
  ask the user before driving mouse and keys, so the two don't collide.
- To find something out, read amiga_screen_text before taking a screenshot; take
  a picture (best of just the part you need) for layout, graphics and games, or
  when the text says something is missing. After a click, amiga_screenshot
  (changed=True) shows only what changed.
- Wait with amiga_wait_window, amiga_wait_text or amiga_wait_change instead of
  screenshots in a loop; put known sequences into one amiga_do call.
- Long commands go through amiga_job, never through amiga_exec; a job that
  outlasts its wait has a number for amiga_jobs, amiga_job_output and
  amiga_break(job=n).
- To find out what really runs (a driver, a library version), amiga_loaded;
  for a crash address, amiga_where, and amiga_memory(show="code") for the code.
- Testing a program: amiga_create_project, amiga_build, amiga_run; amiga_trace
  for where it gets to, amiga_state before and after for leaks, amiga_screen_diff
  for what its screen looks like.
- A hang or stutter: amiga_task(name, stack=8) for what a task waits in,
  amiga_locks for who holds the layer locks, amiga_cpu for the load per task,
  amiga_drag_measure for how a window follows the mouse. A "Software Failure"
  requester: amiga_software_failure, never click Reboot.
- A PowerPC board (CyberStorm PPC, Blizzard PPC): amiga_ppc for the card,
  kernel and ROM; amiga_build(target="warpos") for WarpOS programs.
- amiga_drag says which window moved; if none or another did, its title bar
  was covered. amiga_start(..., unique=True) for programs that run only once.
- Before using a program again, amiga_notes(topic); afterwards amiga_note what
  would save time next time.
- Before installing software, amiga_install_begin, afterwards amiga_install_end,
  so the user can take it back with amiga_install_undo.
- When amiga_status says a newer Amigent is out, tell the user once; only
  after they agree, amiga_update(apply=True) updates this PC and the Amigas."""

mcp = FastMCP("amigent", instructions=INSTRUCTIONS)
# One packing policy per Amiga, shared by channel and screen; amigas.json may set "compress".
sessions = Sessions(CTL_PORT, SCREEN_PORT)

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
    backend = getattr(_session().ctl, "backend", None)
    if not getattr(_session().ctl, "connected", False) or not hasattr(backend, "note"):
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
        return _with_ctl(lambda: _session().ctl.exec(command, timeout=timeout))


# Another agent on the same Amiga: said once per session before input, looked up again
# every few minutes. amiga_status always says it.
INPUT_TOOLS = {"amiga_click", "amiga_type", "amiga_key", "amiga_do", "amiga_start", "amiga_drag",
               "amiga_click_text", "amiga_ui_click", "amiga_menu_select"}
OTHERS_RECHECK = 300.0


def _others_note() -> str:
    """The warning about another running agent, the first time it applies; else ''."""
    if time.monotonic() - _session().others_seen["at"] > OTHERS_RECHECK:
        try:
            info = _with_ctl(_session().ctl.sysinfo)
            _session().others_seen["text"] = others.warning(others.from_sysinfo(info))
        except Exception:  # noqa: BLE001 - never worth a failed tool
            pass
        _session().others_seen["at"] = time.monotonic()
    text = _session().others_seen["text"]
    if not text or _session().others_seen["told"] == text:
        return ""
    _session().others_seen["told"] = text
    return text


_update_told = ""  # the version already mentioned in this session


def _update_note() -> str:
    """Once per session and version: a newer Amigent is out. Looks it up in the background."""
    global _update_told
    update.refresh_in_background()  # at most once a day it really asks; the answer is for next time
    found = update.offer()
    if not found or found["version"] == _update_told:
        return ""
    _update_told = found["version"]
    return (f"Amigent {found['version']} is out (this PC has {found['current']}). Tell the user; "
            "amiga_update() shows what is new, amiga_update(apply=True) installs it once they agree.")


def _outdated_note() -> str:
    """Once per session: 'Note: ...' when the Amiga's amigentd is newer than this PC's Amigent."""
    backend = getattr(_session().ctl, "backend", None)  # tests put simple stand-ins in place
    if _session().outdated_told or backend is None or not getattr(backend, "_sock", None):
        return ""
    _session().outdated_told = True
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
        ok = False
        try:
            result = fn(*args, **kwargs)
            ok = True
        finally:
            if not sent:  # the tool may have brought the link up
                _note(line)
            if fn.__name__ not in UNJOURNALED:
                diary.add(_amiga_name(), line, ok)
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


UNTARGETED = {"amiga_everywhere", "amiga_machines", "amiga_aminet_search", "amiga_aminet_readme", "amiga_update"}


def _tool_noted(*args, **kwargs):
    register = _tool(*args, **kwargs)

    def decorate(fn):
        targeted = fn.__name__ not in UNTARGETED
        signature = inspect.signature(fn)
        if targeted:
            signature = signature.replace(parameters=[*signature.parameters.values(),
                inspect.Parameter("machine", inspect.Parameter.KEYWORD_ONLY, default="", annotation=str)])
        noted = _noted(fn) if targeted else fn

        @functools.wraps(fn)
        def run(*args, **kwargs):
            if not targeted:
                return fn(*args, **kwargs)
            machine = kwargs.pop("machine", "")
            if active.get() is not None and not machine:
                return noted(*args, **kwargs)  # nested operations inherit their caller
            session, label = sessions.resolve(machine)
            with session.calls, bound(session):
                try:
                    session.refresh_address()
                    result = noted(*args, **kwargs)
                except Exception as e:
                    if not label:
                        raise
                    raise AmigaError(f"{label}: {e}", getattr(e, "code", None)) from e
            if not label:
                return result
            if isinstance(result, str):
                return f"{label}:\n{result}"
            return [label, *result] if isinstance(result, list) else result

        run.__signature__ = signature
        if targeted:
            run.__doc__ = (fn.__doc__ or "") + "\n\nmachine: saved Amiga name or ID from amiga_machines; defaults to AMIGENT_HOST, the saved default, or the sole registered machine."

        @functools.wraps(run)
        async def dispatch(*args, **kwargs):
            # FastMCP otherwise executes synchronous functions on its event loop.
            # Separate machines can progress while a slow command waits.
            return await asyncio.to_thread(run, *args, **kwargs)

        register(dispatch)
        return run
    return decorate


mcp.tool = _tool_noted


@mcp.tool()
def amiga_machines() -> str:
    """List saved Amigas with stable IDs, names, addresses and reachability.

    This does not select a target, change addresses or take a screen connection.
    Pass a listed name or ID as machine on subsequent calls.
    """
    from concurrent.futures import ThreadPoolExecutor
    entries = machines.load()
    def status(entry):
        conflicts = machines.conflicts(entry, entries)
        online = not conflicts and net.greet(entry['host'], SCREEN_PORT, 2, connect_timeout=2)[:4] == b'AMGT'
        return {"id": entry['id'], "name": entry['name'], "address": entry['host'],
                "default": bool(entry.get('default')), "online": online,
                "conflicts": conflicts}
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(entries)))) as pool:
        return json.dumps(list(pool.map(status, entries)), indent=1)


def _exclusive():
    """Serialise access across all amigent processes and threads on this PC."""
    return locks.machine(_session().lock_file)


def _screen_lock():
    """The screen service has its own lock, so the viewer keeps running during commands."""
    return locks.machine(_session().lock_file, screen=True)


def _with_ctl(fn):
    """Run fn against amigactld, reconnecting once after a dropped link."""
    with _exclusive():
        if not _session().ctl.connected:
            _session().ctl.connect()
        try:
            return fn()
        except (OSError, EOFError, AmigaError) as e:
            if isinstance(e, AmigaError) and e.code is not None:
                raise  # the daemon answered, this is a real command error
            _session().ctl.close()
            if isinstance(e, TimeoutError):
                # The command reached the Amiga and may still run: repeating it could run it twice.
                raise AmigaError("no answer in time; the command may still be running on the "
                                 "Amiga. Use amiga_job for long work") from e
            try:
                _session().ctl.connect()
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


seen = uptime.Seen()  # under secret.FOLDER, looked up when used
diary = journal.Journal()  # under secret.FOLDER, looked up when used
# Looking around changes nothing on the Amiga; a postmortem wants what did.
UNJOURNALED = {"amiga_status", "amiga_screenshot", "amiga_screenshot_color", "amiga_screen_text",
               "amiga_locks", "amiga_cpu", "amiga_screens", "amiga_ui_tree", "amiga_menus", "amiga_list", "amiga_notes",
               "amiga_installs", "amiga_loaded", "amiga_memory", "amiga_where", "amiga_jobs",
               "amiga_job_output", "amiga_wait_change", "amiga_wait_window", "amiga_wait_text",
               "amiga_tasks", "amiga_gurus", "amiga_state", "amiga_screen_diff", "amiga_debug_log",
               "amiga_emu68", "amiga_listen"}


def _amiga_name() -> str:
    return machines.identity(_session().host) or "amiga"


@mcp.tool()
def amiga_status() -> str:
    """Show memory, OS and TCP stack versions, CPU and FPU, and mounted volumes with free space.

    Also tells whether the Amiga started again since the last look, and the
    guru code it kept from before the last reset, so after a lost connection
    you can tell a crash from a network fault. Call it first when the Amiga
    answers again after being away.
    """
    try:
        info = _with_ctl(_session().ctl.sysinfo)
    except AmigaError as e:
        last = _serial_tail()
        if not last:
            raise
        raise AmigaError(f"{e}\nthe Pi's serial line (recorded on this PC) said last:\n"
                         + "\n".join(last)) from e
    ports, tasks = info.pop("ports", None), info.pop("tasks", None)
    report = {"amiga": machines.name_of(_session().host), "address": machines.address(_session().host)}
    found = others.scan(_Exec(), machines.address(_session().host) or _session().host,
                        {"ports": ports or "", "tasks": tasks or ""})
    _session().others_seen.update(at=time.monotonic(), text=others.warning(found))
    if found:
        report["other_agents"] = [f.as_dict() for f in found]
        if _session().others_seen["text"]:
            report["warning"] = _session().others_seen["text"]
    if "uptime" in info:
        report["running_for"] = uptime.duration(int(info["uptime"]))
    name = machines.identity(_session().host) or ""
    with locks.exclusive("amigent-uptime.lock"):
        before = seen.previous(name)
        said = seen.check(name, info, tasks=tasks)
    if said:
        report["notes"] = said
    if uptime.restarted(said) or info.get("last_alert"):
        report["before_it_went_away"] = _postmortem(before, info, tasks)
    report["system"] = _system_facts()
    if info.get("emu68") == "1":  # amigentd 1.5.0 found Emu68: the Pi's health in one line
        _emu68_status(report)
    report.update({"sysinfo": info, "volumes": _with_ctl(_session().ctl.volumes)})
    outdated = pc_outdated(getattr(getattr(_session().ctl, "backend", None), "protocol", 0))
    if outdated:
        report["update_pc"] = outdated
    newer = _update_note()
    if newer:
        report["amigent_update"] = newer
    serial = _serial_state()
    if serial:
        report["serial_log"] = serial
    if "text" in info:
        report["recording"] = {"screen_text": info.get("text") == "1",
                               "debug_output": info.get("debug") == "1"}
        report["packing"] = pack.policy_for(_session().host).describe()
    return json.dumps(report, indent=1)


def _emu68_status(report: dict) -> None:
    from . import emu68
    try:
        pi = _emu68_info()
    except AmigaError:
        return
    if not emu68.present(pi):
        return
    report["emu68"] = (pi.get("idstring") or "Emu68") + (f" on {pi['model']}" if pi.get("model") else "")
    warn = emu68.health_warning(pi)
    if warn:
        report["pi_warning"] = warn


def _postmortem(before: dict, info: dict, tasks: str | None) -> dict:
    """After a restart: what the agent did last, and what ran then that doesn't now."""
    out = {}
    try:
        started = time.time() - int(info.get("uptime", 0))
    except ValueError:
        started = None
    last = diary.recent(_amiga_name(), before=started)
    if last:
        out["last_actions"] = journal.describe(last)
    then, now = others.names(before.get("tasks", "")), others.names(tasks or "")
    gone = sorted(then - now)
    if gone:
        out["running_then_not_now"] = gone
    serial = _serial_tail(before=started) if started else []
    if serial:
        out["serial_before_restart"] = serial
    if not out:
        out["note"] = "nothing recorded from before; the journal starts with this version"
    return out




def _serial_tail(lines: int = 20, before: float | None = None) -> list[str]:
    """The last lines the Pi's serial line gave (amigent-serial or amiga_serial_log), if recorded."""
    try:
        return serial_log.tail(_session().host, lines, before=before)
    except (OSError, ValueError):
        return []


def _serial_state() -> dict:
    """For amiga_status: whether the serial line is recorded, only when it ever was."""
    try:
        if not serial_log.exists(_session().host):
            return {}
        state = serial_log.status(_session().host)
    except (OSError, ValueError):
        return {}
    return {k: state[k] for k in ("recording", "port", "last_data", "file") if k in state}


def _system_facts() -> dict:
    """OS, CPU and PowerPC software, asked once per session; the system doesn't change while it runs."""
    if not _session().system_seen:
        run = lambda c: (lambda r: (r.rc, r.output))(_with_ctl(lambda: _session().ctl.exec(c)))  # noqa: E731
        facts = system.describe_system(run("Version")[1], run("CPU")[1])
        try:
            facts = system.reconcile_system(facts, _with_ctl(_session().ctl.sysinfo))
        except AmigaError:
            pass
        if system.native_ppc(facts):
            facts["note"] = system.EMULATED_NOTE
        else:
            ppc = system.ppc_software(run)
            if ppc:
                facts["ppc"] = ppc
            fpu = _read_fpu(facts)
            facts.update(fpu=fpu.short(), processor=fpu.line())
            _session().fpu = fpu
        _session().system_seen.update({k: v for k, v in facts.items() if v})
    return dict(_session().system_seen)


def _read_fpu(facts: dict) -> "system.Fpu":
    """The FPU from exec's AttnFlags (SYSINFO), and on a 68040/060 whether its library is loaded."""
    try:
        flags = system.attn_flags(_with_ctl(_session().ctl.sysinfo))
    except (AmigaError, AttributeError):
        flags = None
    fpu = system.describe_fpu(flags, facts.get("cpu", ""), facts.get("fpu", ""))
    if system.wants_loaded(fpu):
        try:
            system.apply_loaded(fpu, _loaded().libraries)
        except (AmigaError, AttributeError):
            pass  # amigentd before 1.4.0, or amigactld: the flags alone tell
    return fpu


def _fpu() -> "system.Fpu | None":
    """This Amiga's FPU, asked once per session; None on PowerPC systems or when it cannot be told."""
    try:
        _system_facts()
    except AmigaError:
        return None
    return _session().fpu


def _fpu_hint(code) -> str:
    """For a line-F guru or an FPU support trap: what this Amiga's FPU says about it."""
    from . import alerts
    if alerts.cpu_exception(code) not in (alerts.LINE_F, *alerts.SUPPORT_TRAPS):
        return ""
    return alerts.fpu_hint(code, _fpu())


def _clear_hold() -> str:
    """Before a window opens: get Intuition going again, or say what holds it up."""
    try:
        state = _with_screen(_session().screen.held)
    except (OSError, AmigaError):
        return ""
    done = ""
    if state == "held":  # a popup left open, such as Directory Opus's after a short right click
        _with_screen(lambda: _session().screen.press("esc"))
        done = "closed a popup with Esc first. "
    elif state == "menu" and _session().screen.right_held():  # our own right button holds a menu open
        _with_screen(lambda: _session().screen.button("right", False))
        done = "let go of the right mouse button first. "
    else:
        return HOLD_NOTE[state] if state else ""
    time.sleep(0.3)
    state = _with_screen(_session().screen.held)
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
    if not _session().host:
        return "No Amiga known yet: run amigent-setup."
    before = machines.address(_session().host)
    if address:
        machines.move(_session().host, address.strip())
        found = address.strip()
    else:
        machines.forget_search(_session().host)
        found = machines.relocate(_session().host)
    _session().ctl.close()
    _session().screen.close()
    if not found:
        return (f"The Amiga does not answer at {before} and is not elsewhere on this network. "
                "Is it switched on and amigentd running? Ask the user for its address.")
    return f'"{machines.name_of(_session().host)}" is at {found} now (was {before}).'


@mcp.tool()
def amiga_exec(command: str, cd: str = "", timeout: float = 30) -> str:
    """Run a short AmigaDOS command and return rc and output.

    Blocks the Amiga daemon for everyone until it returns. Use amiga_job for
    anything that may take long, and amiga_start for graphical programs.
    """
    if SLOW_COMMAND.search(command):
        raise AmigaError("recursive scans can block the Amiga for many minutes; use amiga_job")
    result = _with_ctl(lambda: _session().ctl.exec(command, cd or None, timeout))
    return f"rc={result.rc}\n{result.output}"


START_WINDOWS = 30.0  # s: wait_window(new) counts windows opened since a start this recent


@mcp.tool()
def amiga_start(command: str, unique: bool = False) -> str:
    """Start a program in the background, without capturing output.

    unique=True first looks whether the program runs already and then starts
    nothing: many programs open an "already running" requester instead, which
    is easy to miss. Shows what amiga_note recorded about the program earlier.
    """
    if unique:
        running = _running_as(command)
        if running:
            return (f"not started: {command.split()[0]} runs already as " + ", ".join(
                f"{t.label} (${t.address:08X})" for t in running)
                + "; amiga_tasks shows it, amiga_task(..., signal='C') asks it to quit")
    before = _clear_hold()
    _session().windows_at_start = (time.monotonic(), _window_ids())
    _with_ctl(lambda: _session().ctl.exec(f"Run >NIL: {command}"))
    _session().last_started = command
    known = notes.for_amiga(_amiga()).get(notes.topic_of(command))
    hints = ("\nnotes on it from earlier:\n" + "\n".join(f"- {n}" for n in known)) if known else ""
    return f"{before}started: {command}{hints}"


def _end_broken_script(job: int, process: str) -> str:
    """After Ctrl-C and Ctrl-D for a job's script: the script never gets to the line
    that writes its return code, so that is done here once its Shell is gone."""
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        done, rc, status = _with_ctl(lambda: perform(_session().ctl, [
            Request.read_file(_job_file(job, "done")), Request.read_file(_job_file(job, "rc")),
            Request.exec("Status")]))
        if not isinstance(done, AmigaError) or not isinstance(rc, AmigaError):
            return ""  # its last line ended by itself
        listed = "" if isinstance(status, AmigaError) else status.output
        # "Process 4: Loaded as command: Wait", in any language. A freed number goes to
        # the next Shell at once, and that is this Status itself.
        line = re.search(rf"^\s*\S+\s+{process}:.*$", listed, re.M)
        if not line or line.group(0).rstrip().lower().endswith("status"):
            _with_ctl(lambda: _session().ctl.write_file(_job_file(job, "done"), b"20\n"))
            return " and Ctrl-D to its script, which ended"
        time.sleep(0.5)
    return " and Ctrl-D to its script; the line that runs has not ended yet"


@mcp.tool()
def amiga_break(command: str = "", signal: str = "C", job: int = 0) -> str:
    """Send Ctrl-C (or D, E, F) to a running command, like pressing it in its Shell.

    command is the program's name as Status shows it, e.g. 'List' or 'Wait';
    job is a number from amiga_job (a job of several lines is stopped as a
    whole). Without either, the command last started by
    amiga_job or amiga_start is meant. A plain amiga_exec cannot be broken this
    way: it blocks the daemon until it returns, so long work belongs in amiga_job.
    """
    if signal.upper() not in ("C", "D", "E", "F"):
        raise AmigaError("signal must be C, D, E or F")
    if job:
        said = _with_ctl(lambda: _session().ctl.read_file(_job_file(job, "cli"))).decode("iso-8859-1")
        m = re.search(r"\[CLI (\d+)\]", said)
        if not m:
            raise AmigaError(f"job {job}: no process number recorded ({said.strip() or 'empty'})")
        script = False
        if signal.upper() == "C":
            try:  # a job of several lines is a script: Ctrl-C alone only ends the line that runs
                _with_ctl(lambda: _session().ctl.stat(_job_file(job, "lines")))
                script = True
            except AmigaError:
                pass
        _with_ctl(lambda: _session().ctl.exec(f"Break {m.group(1)} {'C D' if script else signal.upper()}"))
        said = f"sent Ctrl-{signal.upper()} to job {job} (process {m.group(1)})"
        return said + _end_broken_script(job, m.group(1)) if script else said
    name = command or _session().last_started
    if not name:
        raise AmigaError("nothing started yet; give the command's name")
    wanted = PurePosixPath(name.split()[0].split(":")[-1]).name.lower()
    status = _with_ctl(lambda: _session().ctl.exec("Status")).output
    hits = []
    for line in status.splitlines():
        m = re.match(r"\s*Process\s+(\d+):.*?command:\s*(\S+)", line)
        if m and PurePosixPath(m.group(2).split(":")[-1]).name.lower() == wanted:
            hits.append(m.group(1))
    if not hits:
        return f"no running process named {wanted}"
    for number in hits:
        _with_ctl(lambda: _session().ctl.exec(f"Break {number} {signal.upper()}"))
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
    wrote, result = _with_ctl(lambda: perform(_session().ctl, [
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


JOB_BASE = "RAM:amigent-job-"


def _job_file(number: int, kind: str) -> str:
    return f"{JOB_BASE}{number}.{kind}"


def _known_jobs() -> list[int]:
    """Job numbers that left files in RAM:, this session's or an earlier one's."""
    try:
        entries = _with_ctl(lambda: _session().ctl.list_dir("RAM:"))
    except AmigaError:
        return []
    found = set()
    for e in entries:
        m = re.fullmatch(r"amigent-job-(\d+)\.cmd", e.get("name", ""), re.I)
        if m:
            found.add(int(m.group(1)))
    return sorted(found)


def _next_job() -> int:
    if not _session().job_numbers:
        _session().job_numbers.append(max(_known_jobs() or [0]))
    _session().job_numbers[0] += 1
    return _session().job_numbers[0]


def _job_result(number: int, rc_text: str, output: bytes) -> str:
    text = output.decode("iso-8859-1")
    rc = rc_text.strip()
    if rc and rc != "0":
        text += f"{'' if text.endswith(chr(10)) or not text else chr(10)}[job {number} ended with rc {rc}]"
    return text




def _amigentd_path() -> str:
    """Where amigentd lives on the Amiga (SYSINFO self=, 1.4.0), for its FOLLOW helper;
    '' when it can't help, also when the Amiga has no PIPE: (a bare Workbench disk)."""
    if "path" not in _session().self_path:
        try:
            me = _with_ctl(_session().ctl.sysinfo).get("self", "")
        except AmigaError:
            return ""
        if me:
            try:
                _with_ctl(lambda: _session().ctl.stat(me))
            except AmigaError:
                me = ""  # a self= that is not there (a build of its own): jobs as before 1.4.0
        if me:
            try:
                _with_ctl(lambda: _session().ctl.stat("PIPE:"))
            except AmigaError as e:
                if "not mounted" in str(e) or "isn't there" in str(e):
                    me = ""  # no queue-handler: jobs as before 1.4.0
        _session().self_path["path"] = me
    return _session().self_path["path"]


@mcp.tool()
def amiga_job(command: str, wait: float = 600) -> str:
    """Run a long command at low priority in the background and return its output.

    The Amiga stays reachable meanwhile, and several jobs can run at once. If
    wait runs out, the job keeps going: the answer gives its number, for
    amiga_jobs (all of them), amiga_job_output (what it printed so far) and
    amiga_break(job=n). A return code other than 0 is said at the end.
    """
    number = _start_job(command)
    said = _wait_job(number, wait)
    if said is not None:
        return said
    return (f"job {number} still running; amiga_job_output({number}) shows what it printed, "
            f"amiga_break(job={number}) stops it")


def _start_job(command: str, title: str = "") -> int:
    """Starts command (one or more lines) as a background job; its number.
    title stands in amiga_jobs instead of the command."""
    number = _next_job()
    out, done, cli = (_job_file(number, k) for k in ("out", "done", "cli"))
    lines = [x for x in command.strip().splitlines() if x.strip()]
    extra = []
    if len(lines) > 1:  # a redirection only takes one line: several go as a script of their own
        extra = [Request.write(_job_file(number, "lines"), ("\n".join(lines) + "\n").encode("iso-8859-1", "replace"))]
        command = f"Execute {_job_file(number, 'lines')}"
    me = _amigentd_path()
    if me:  # 1.4.0: output through a pipe, readable while the job runs
        pipe = f"PIPE:amigent-job-{number}"
        script = (
            "FailAt 21\nChangeTaskPri -5\n"
            f'Run >NIL: "{me}" FOLLOW {pipe} TO {out}\n'
            f"{command} >{pipe}\n"
            f'Echo >{_job_file(number, "rc")} "$RC"\n'
        )
    else:
        script = (
            "FailAt 21\nChangeTaskPri -5\n"
            f"{command} >>{out}\n"
            f'Echo >{done} "$RC"\n'
        )
    answers = _with_ctl(lambda: perform(_session().ctl, extra + [
        Request.write(_job_file(number, "cmd"), (title or "; ".join(lines)).encode("iso-8859-1", "replace")),
        Request.write(out, b""),
        Request.write(_job_file(number, "script"), script.encode("iso-8859-1")),
        Request.exec(f"Run >{cli} Execute {_job_file(number, 'script')}")]))
    for answer in answers:
        if isinstance(answer, AmigaError):
            raise answer
    _session().last_started = command
    return number


def _wait_job(number: int, wait: float) -> str | None:
    """The job's output with its return code once it ended within wait seconds, else None."""
    out, done = _job_file(number, "out"), _job_file(number, "done")
    deadline = time.monotonic() + wait
    pause = 0.5  # short jobs are common: look soon, then back off to 5 s
    seen_rc = 0.0
    while time.monotonic() < deadline:
        # marker and output in one go: a finished job costs one round trip, not two
        rc, output, early = _with_ctl(lambda: perform(_session().ctl, [
            Request.read_file(done), Request.read_file(out), Request.read_file(_job_file(number, "rc"))]))
        if isinstance(rc, AmigaError) and not isinstance(early, AmigaError):
            # the command ended; FOLLOW renames rc to done after its last piece. If it
            # never does (it could not start), the rc file says enough after a moment.
            seen_rc = seen_rc or time.monotonic()
            if time.monotonic() - seen_rc > 5:
                rc = early
        if not isinstance(rc, AmigaError):
            if isinstance(output, AmigaError):
                raise output
            return _job_result(number, rc.decode("iso-8859-1"), output)
        time.sleep(min(pause, max(deadline - time.monotonic(), 0)))
        pause = min(pause * 2, 5)
    return None


def _job_ended(number: int) -> str | None:
    """The return code of a job that ended, None while it runs."""
    done, rc = _with_ctl(lambda: perform(_session().ctl, [
        Request.read_file(_job_file(number, "done")), Request.read_file(_job_file(number, "rc"))]))
    for answer in (done, rc):
        if not isinstance(answer, AmigaError):
            return answer.decode("iso-8859-1").strip() or "?"
    return None


@mcp.tool()
def amiga_jobs(clear_finished: bool = False) -> str:
    """Background jobs started by amiga_job: number, running or rc, output size, command.

    clear_finished removes the files of jobs that ended.
    """
    numbers = _known_jobs()
    if not numbers:
        return "no jobs"
    reqs = []
    for n in numbers:
        reqs += [Request.read_file(_job_file(n, "cmd")), Request.read_file(_job_file(n, "done")),
                 Request.stat(_job_file(n, "out")), Request.read_file(_job_file(n, "rc"))]
    answers = _with_ctl(lambda: perform(_session().ctl, reqs))
    lines, finished = [], []
    for i, n in enumerate(numbers):
        cmd, done, out, rc = answers[4 * i:4 * i + 4]
        if isinstance(done, AmigaError) and not isinstance(rc, AmigaError):
            done = rc  # the command ended; only the copy of its output may still be on its way
        state = "running" if isinstance(done, AmigaError) else f"rc {done.decode().strip() or '?'}"
        size = out.get("size", 0) if isinstance(out, dict) else 0
        command = cmd.decode("iso-8859-1") if isinstance(cmd, bytes) else "?"
        lines.append(f"{n:4}  {state:9} {size:>8} bytes  {command}")
        if state != "running":
            finished.append(n)
    if clear_finished and finished:
        names = " ".join(f"{JOB_BASE}{n}.#?" for n in finished)
        _with_ctl(lambda: _session().ctl.exec(f"Delete >NIL: {names} QUIET"))
        lines.append(f"cleared {len(finished)} finished job(s)")
    return "\n".join(lines)


@mcp.tool()
def amiga_job_output(job: int, last: int = 4000) -> str:
    """What a background job printed so far, the last bytes of it (0 for all)."""
    try:
        data = _with_ctl(lambda: _session().ctl.read_file(_job_file(job, "out")))
    except AmigaError as e:
        if "open for writing" not in str(e):  # DOS_HINTS[202]: the Shell holds the file
            raise
        return (f"[job {job} still running; its output is held by the Shell until it ends, "
                "with amigentd 1.4.0 on the Amiga it can be read while it runs]")
    if last and len(data) > last:
        data = b"..." + data[-last:]
    state = f"[job {job} still running]"
    for kind in ("done", "rc"):
        try:
            rc = _with_ctl(lambda: _session().ctl.read_file(_job_file(job, kind))).decode().strip()
            state = f"[job {job} ended with rc {rc}]"
            break
        except AmigaError:
            pass
    return data.decode("iso-8859-1") + ("" if data.endswith(b"\n") or not data else "\n") + state


def _loaded() -> "memory.Loaded":
    return memory.parse(_with_ctl(_session().ctl.loaded))


def _namer() -> "memory.Namer":
    return memory.Namer(_loaded(), peek=lambda a, n: _with_ctl(lambda: _session().ctl.peek(a, n)))


@mcp.tool()
def amiga_loaded(kind: str = "all", name: str = "") -> str:
    """What is in memory now: libraries, devices, resources with version and open
    count; running programs with their hunks; resident modules.

    kind: all, libraries, devices, resources, programs or residents; name filters.
    Tells which driver really runs (an AHI driver, a disk device), unlike the
    files on disk. Needs amigentd 1.4.0.
    """
    if kind not in ("all", "libraries", "devices", "resources", "programs", "residents"):
        raise AmigaError("kind: all, libraries, devices, resources, programs or residents")
    return memory.listing(_loaded(), kind, name)


@mcp.tool()
def amiga_where(address: str) -> str:
    """Name the code or data at an address: program and hunk, library function,
    resident module or ROM. For a guru's address, a debugger's or CacheCop's PC.
    Needs amigentd 1.4.0."""
    namer = _namer()
    try:
        at = memory.resolve(address, namer)
    except ValueError as e:
        raise AmigaError(str(e)) from None
    return f"${at:08X}: {namer.name(at) or 'not in a program, library or module amigentd knows'}"


@mcp.tool()
def amiga_memory(address: str = "", size: int = 256, show: str = "hex", a6: str = "",
                 task: str = "", hunk: int | None = None, offset: str = "") -> str:
    """Read the Amiga's memory, without changing anything.

    address: hex ($0A12B4C0, 0x...), a library ('dos.library' for its base), a
    function ('dos.library/Write' for its code), or a running program's hunk
    ('ProTracker:hunk1+$120', 'ProTracker hunk 1 +$120', offset in hex). Or
    task (name, program or address from amiga_tasks), hunk (from 0) and offset
    ($120, 0x120 or decimal) instead of address. A hunk read stays inside the
    hunk: an offset past its end is refused, a size past it cut short. show: hex (dump with text),
    code (68k disassembly with library function names; a6 names the library a6
    holds, e.g. 'dos'), or text (the bytes as text up to a zero byte). Only RAM
    that exec manages and the Kickstart ROM, at most 64 KB; custom chips, CIAs
    and boards are refused, since reading them can change what they do.
    Needs amigentd 1.4.0.
    """
    namer = _namer()
    cut = ""
    try:
        if task or hunk is not None:
            if address:
                raise ValueError("give address, or task with hunk and offset; not both")
            if not task or hunk is None:
                raise ValueError("task= and hunk= go together, offset= is optional")
            h, off = _task_hunk(namer, task, hunk), memory.parse_offset(offset)
        elif offset:
            raise ValueError("offset= counts from a hunk: give task= and hunk= as well")
        elif not address:
            raise ValueError("address, or task with hunk and offset")
        elif parts := memory.split_hunk(address):
            h, off = _task_hunk(namer, parts[0], parts[1]), parts[2]
        else:
            h = None
            at = memory.resolve(address, namer)
        if h is not None:
            if not 0 <= off < h.size:
                raise ValueError(f"hunk {h.number} of {h.command or h.task} is ${h.size:X} bytes; "
                                 f"offset ${off:X} is outside it")
            at = h.start + off
            if off + size > h.size:
                size = h.size - off
                cut = f"; {size} bytes, up to the end of hunk {h.number}"
    except ValueError as e:
        raise AmigaError(str(e)) from None
    data = _with_ctl(lambda: _session().ctl.peek(at, size))
    head = f"${at:08X}" + (f" ({namer.name(at, search_functions=False)})"
                           if namer.name(at, search_functions=False) else "") + cut
    if show == "code":
        cpu = str(_system_facts().get("cpu", "68040"))
        cpu = cpu if cpu in ("68000", "68010", "68020", "68030", "68040", "68060") else "68040"
        return head + "\n" + memory.disassemble(data, at, a6, namer, cpu)
    if show == "text":
        return head + "\n" + data.split(b"\0", 1)[0].decode("iso-8859-1")
    return head + "\n" + memory.hexdump(data, at)


def _task_hunk(namer: "memory.Namer", who: str, number: int) -> "memory.Hunk":
    """A running program's hunk; who by task address goes through the task list."""
    w = who.strip()
    if w.startswith(("$", "0x", "0X")) or re.fullmatch(r"[0-9a-fA-F]{8}", w):
        return memory.find_hunk(namer.loaded, w, number, task=memory.find_task(_tasks(), w))
    return memory.find_hunk(namer.loaded, w, number)


def _tasks() -> list:
    return memory.parse_tasks(_with_ctl(_session().ctl.tasks))


def _running_as(command: str) -> list:
    """Tasks that run the program command starts, by its file name."""
    word = command.strip().strip('"').split()[0] if command.strip() else ""
    want = word.rsplit("/", 1)[-1].rsplit(":", 1)[-1].lower()
    if not want:
        return []
    try:
        tasks = _tasks()
    except AmigaError:
        return []  # an amigentd before 1.4.0: start as before
    return [t for t in tasks if want in (memory.program_name(t.command), t.name.lower(),
                                         memory.program_name(t.name))]


@mcp.tool()
def amiga_tasks(name: str = "") -> str:
    """All tasks and processes: address, priority, ready or waiting, stack used of
    its size (with a warning near full), and the program. name filters.
    Needs amigentd 1.4.0. With WarpOS running, the PowerPC tasks follow
    with their CPU load, as WarpOS's tool stat tells it."""
    table = memory.task_table(_tasks(), name)
    extra = _ppc_task_section(name)
    return table + ("\n\n" + extra if extra else "")


WARPOS_TOOLS_ENV = "AMIGENT_WARPOS_TOOLS"  # a folder on the Amiga with showinfo and stat


def _run_rc(command: str) -> tuple[int, str]:
    result = _with_ctl(lambda: _session().ctl.exec(command))
    return result.rc, result.output


def _read_text(path: str) -> str | None:
    try:
        return _with_ctl(lambda: _session().ctl.read_file(path)).decode("iso-8859-1")
    except AmigaError as e:
        if e.code is None:
            raise
        return None


def _ppc_task_section(name: str = "", folder: str = "") -> str:
    """PowerPC tasks with their load, only where WarpOS is in LIBS: and in memory; else ''."""
    from . import ppc
    try:
        if "WarpOS" not in _system_facts().get("ppc", ""):
            return ""
        kern = ppc.kernel(_loaded().libraries)
        return ppc.ppc_tasks(_run_rc, kern, folder or os.environ.get(WARPOS_TOOLS_ENV, ""), name)
    except (AmigaError, AttributeError):
        return ""


@mcp.tool()
def amiga_task(task: str, priority: int | None = None, signal: str = "", stack: int = 0) -> str:
    """Look at or change a task: priority (-128..127, 0 is normal; above 5 can
    starve the system) or signal it with Ctrl-C, D, E or F (e.g. 'C', or 'CD').
    stack=N shows what it waits for (SigWait, SigRecvd) and up to N return
    addresses on its stack, named: for a task that hangs or two that wait for
    each other. task is a name, a program as amiga_tasks shows it, or an
    address. Needs amigentd 1.4.0."""
    tasks = _tasks()
    try:
        t = memory.find_task(tasks, task)
    except ValueError as e:
        raise AmigaError(str(e)) from None
    done = []
    if priority is not None:
        if not -128 <= priority <= 127:
            raise AmigaError("priority goes from -128 to 127")
        _with_ctl(lambda: _session().ctl.task_control(t.address, 1, priority))
        done.append(f"priority {t.pri} -> {priority}")
    if signal:
        bits = 0
        for ch in signal.upper():
            if ch not in "CDEF":
                raise AmigaError("signal: letters C, D, E, F")
            bits |= 1 << "CDEF".index(ch)
        _with_ctl(lambda: _session().ctl.task_control(t.address, 2, bits))
        done.append("sent Ctrl-" + "+".join(sorted(set(signal.upper()))))
    if stack > 0:
        return memory.task_table([t]) + "\n" + _backtrace(t, stack)
    if not done:
        return memory.task_table([t])
    return f"{t.label} (${t.address:08X}): " + ", ".join(done)


def _backtrace(t, most: int) -> str:
    """SigWait, SigRecvd and the named return addresses on a task's stack."""
    head = _with_ctl(lambda: _session().ctl.peek(t.address + 54, 12))  # tc_SPReg, tc_SPLower, tc_SPUpper
    sp, lower, upper = (int.from_bytes(head[i:i + 4], "big") for i in (0, 4, 8))
    lines = [f"waits for {memory.signal_names(t.sig_wait)}; got {memory.signal_names(t.sig_recvd)}"]
    if t.state == "run":
        lines.append("it runs right now (it is amigentd's own channel); its saved stack pointer is old")
    if not lower <= sp < upper:
        return "\n".join(lines + [f"stack pointer ${sp:08X} is outside its stack "
                                  f"${lower:08X}-${upper:08X}: overflow or a stack of its own"])
    size = min(upper - sp, 16384)
    data = _with_ctl(lambda: _session().ctl.peek(sp, size))
    namer = _namer()

    def check(addresses):
        got = _with_ctl(lambda: _session().ctl.peek_many([(a - 6, 6) for a in addresses]))
        return [g if isinstance(g, bytes) else None for g in got]

    calls = memory.backtrace(data, sp, namer, check, most)
    lines.append(f"stack ${lower:08X}-${upper:08X}, sp ${sp:08X}, {upper - sp} bytes in use"
                 + (f", first {size} read" if size < upper - sp else ""))
    lines += ["return addresses, innermost first:"] + calls if calls else \
        ["no return addresses found (code not in a program, library or the ROM amigentd knows)"]
    return "\n".join(lines)


TASK_POLL = (1.0, 3.0)  # seconds between looks at the task list: first, at most


def _task_hits(tasks: list, which: str) -> list:
    """Tasks that match like amiga_task: an address, else name or program."""
    w = which.strip()
    if w.startswith(("$", "0x")) or re.fullmatch(r"[0-9a-fA-F]{8}", w):
        try:
            return [memory.find_task(tasks, w)]
        except ValueError:
            return []
    return memory.match_tasks(tasks, w)


@mcp.tool()
def amiga_wait_task(name: str, gone: bool = False, timeout: float = 30) -> str:
    """Wait until a task or process with this name runs (or, gone=True, until none does).

    For programs a script started with Run: amiga_exec returns when the script
    ends, this waits for what it started. name matches like amiga_task: a task
    name, a program as amiga_tasks shows it (its file name is enough), or an
    address. Looks at the task list every one to three seconds, so it is gentle
    on a slow line. Needs amigentd 1.4.0.
    """
    if not name.strip():
        raise AmigaError("name: a task name, a program or an address")
    timeout = max(0.0, min(float(timeout), 3600.0))
    start, pause, first = time.monotonic(), TASK_POLL[0], True
    while True:
        hits = _task_hits(_tasks(), name)
        took = time.monotonic() - start
        if gone and not hits:
            return (f"no task matches {name!r}: none was running" if first else
                    f"no task matches {name!r} any more; gone after {took:.0f} s")
        if hits and not gone:
            return (f"{len(hits)} task(s) match {name!r}" + (f" after {took:.0f} s" if not first else "")
                    + ":\n" + memory.task_table(hits))
        if took >= timeout:
            if gone:
                return (f"timeout: {len(hits)} task(s) matching {name!r} still there after {timeout:g} s:\n"
                        + memory.task_table(hits))
            return f"timeout: no task matches {name!r} after {timeout:g} s"
        first = False
        time.sleep(min(pause, max(0.0, timeout - took)))
        pause = min(pause * 1.5, TASK_POLL[1])


@mcp.tool()
def amiga_amigus_status() -> str:
    """The AmiGUS sound card, read only: is a card there, are amigus.library and
    the AHI driver AmiGUS.audio loaded, and which parts (PCM, wavetable, codec)
    are reserved right now, by whom.

    Reads amigus.library's own records, so nothing is reserved to find out.
    Says clearly when there is no AmiGUS. Needs amigentd 1.4.0.
    """
    from . import amigus
    loaded = _loaded()
    try:
        tasks = _tasks()
    except AmigaError:
        tasks = []
    ctl = _session().ctl
    sysbase = int.from_bytes(_with_ctl(lambda: ctl.peek(4, 4)), "big")
    return amigus.status(loaded, tasks, lambda a, n: _with_ctl(lambda: ctl.peek(a, n)), sysbase)


@mcp.tool()
def amiga_ppc(action: str = "status", tools: str = "") -> str:
    """A PowerPC board (CyberStorm PPC, Blizzard PPC), read only.

    action status: the card (from expansion.library), the 68k CPU beside it,
    which kernel runs (WarpOS powerpc.library or PowerUP ppc.library, with
    versions; ppclibemu and powerpc.library V7 told apart), the flash ROM's
    version, 68040/68060.library, PPC patches in the startup scripts, and
    combinations known not to work. With WarpOS running also the PowerPC
    model and clock from WarpOS's showinfo.
    action tasks: the PowerPC tasks with their CPU load (WarpOS's stat).
    action crash: the WarpOS or PowerUP crash window in words (as
    amiga_software_failure does it); 'dismiss' closes that window.
    tools: the folder of WarpOS's tools on the Amiga, if they are not along
    the path (or set AMIGENT_WARPOS_TOOLS). Nothing that was not active is
    opened or loaded, and the flash ROM is never written. Not tried on a real
    board yet. Needs amigentd 1.4.0.
    """
    from . import ppc
    folder = tools or os.environ.get(WARPOS_TOOLS_ENV, "")
    if action in ("crash", "dismiss"):
        return _ppc_crash(_screen_text(), "show" if action == "crash" else "dismiss") or \
            "no PowerPC crash window on the front screen (amiga_screen_text shows what is there)"
    if action == "tasks":
        kern = ppc.kernel(_loaded().libraries)
        if not ppc.warpos_running(kern):
            return ("WarpOS is not in memory, so there are no PowerPC tasks it could tell"
                    + (" (PowerUP runs; Amigent reads no PowerUP tool yet)" if kern.active == "PowerUP" else ""))
        return ppc.ppc_tasks(_run_rc, kern, folder)
    if action != "status":
        raise AmigaError("action: status, tasks, crash or dismiss")
    facts = _system_facts()
    if system.native_ppc(facts):
        return f"{facts.get('os')} runs on the PowerPC itself; amiga_ppc is for 68k Amigas with a PowerPC board"
    loaded = _loaded()
    ctl = _session().ctl
    return ppc.status(loaded, lambda a, n: _with_ctl(lambda: ctl.peek(a, n)), _run_rc, _read_text,
                      facts.get("processor", "") or facts.get("cpu", ""), facts.get("ppc", ""), folder)


def _ppc_crash(st, action: str = "show") -> str:
    """A WarpOS or PowerUP crash window in words, and with dismiss its window closed; '' if none."""
    from . import ppc
    wins = [w for w in st.windows if not w.monitor
            and ppc.is_crash_window(w.title, "\n".join(r.text for r in w.rows))]
    if not wins:
        return ""
    w = wins[0]
    text = "\n".join(r.text for r in w.rows)
    crash = ppc.parse_crash(w.title, text)
    try:
        namer = _namer().name
    except (AmigaError, AttributeError):
        namer = None
    out = [f"PowerPC crash window {w.title!r} {w.id}:", text] + ppc.describe_crash(crash, namer)
    if action == "show":
        return "\n".join(out)
    tree = _with_screen(_session().screen.ui_tree)
    mine = [g for g in tree if g["kind"] == "gadget" and g["window"] == w.id]
    close = next((g for g in mine if g["type"] == "sys" and g["label"] == "close"), None)
    if close:
        x, y = _centre(close)
        _with_screen(lambda: _session().screen.click(x, y, "left", False))
        return "\n".join(out + ["closed the crash window; the crashed PPC task stays as it is"])
    buttons = [g for g in mine if g["type"] != "sys"]
    _labels_from_text(buttons)
    for g in buttons:
        label = g["label"].lower().strip("_ ")
        if any(b in label for b in SAFE_BUTTONS) and not any(b in label for b in REBOOT_BUTTONS):
            x, y = _centre(g)
            _with_screen(lambda: _session().screen.click(x, y, "left", False))
            return "\n".join(out + [f"clicked {g['label']!r}; the Amiga runs on"])
    raise AmigaError("found no close gadget or Suspend button on the PowerPC crash window; not "
                     "clicking anything else:\n" + "\n".join(out))


LEVELS_HELPER = "T:amigent-levels"


@mcp.tool()
def amiga_audio_levels(seconds: int = 2, hz: int = 10, peak: bool = True) -> str:
    """Left and right output level of an AmiGUS card over time, read back from the card.

    seconds 1-30, hz 1-50 samples a second; peak=True takes the maximum of each
    interval, else the average. Shows for each channel whether it carries
    sound, with a series in percent of full scale: e.g. to hear whether two
    players play at once. Reads the card's level registers as the AmiGUS
    Mixer does, reserves nothing, so it works while the AHI driver plays.
    Copies the small program amigent-levels to T: and runs it.
    """
    from . import amigus
    from .paths import AMIGA
    seconds, hz = max(1, min(int(seconds), 30)), max(1, min(int(hz), 50))
    local = AMIGA / "bin" / "amigent-levels"
    if not local.exists():
        raise AmigaError("amigent-levels is not part of this Amigent package; build it from "
                         "amiga/amigent-levels.c (see the note at its top)")
    data = local.read_bytes()
    ctl = _session().ctl
    try:
        there = _with_ctl(lambda: ctl.read_file(LEVELS_HELPER))
    except AmigaError:
        there = None
    if there != data:
        _with_ctl(lambda: ctl.write_file(LEVELS_HELPER, data))
    command = f"{LEVELS_HELPER} SECONDS {seconds} HZ {hz}" + (" PEAK" if peak else "")
    result = _with_ctl(lambda: ctl.exec(command, None, seconds + 30))
    return amigus.levels(result.output, seconds, hz, peak)


def _graphics_info() -> "graphics.Info":
    """Board, driver and 3D libraries; remembered for the screenshot note."""
    from . import graphics
    ctl = _session().ctl
    try:
        loaded = _loaded()
    except AmigaError:
        loaded = None  # amigentd before 1.4.0: no LOADED, no PEEK
    peek = (lambda a, n: _with_ctl(lambda: ctl.peek(a, n))) if loaded else None
    run = lambda c: (lambda r: (r.rc, r.output))(_with_ctl(lambda: ctl.exec(c)))  # noqa: E731
    info = graphics.detect(loaded, peek, run, lambda p: _with_ctl(lambda: ctl.list_dir(p, recursive=True)))
    _session().graphics = info
    return info


@mcp.tool()
def amiga_graphics() -> str:
    """The graphics board, read only: Picasso96 or CyberGraphX with versions, the
    card driver (VideoCore.card, uaegfx.card, ZZ9000.card, PicassoIV.card, ...) and
    chip driver in memory, the graphics boards in the Zorro list with their bus,
    and for 3D Warp3D, its drivers and MiniGL, in memory or in LIBS:.

    Says whether the card sits behind a slow bus (Zorro II), where screen reads
    cost more. Screen modes: amiga_screen_modes.
    """
    from . import graphics
    return graphics.describe(_graphics_info())


def _p96_settings(path: str) -> bytes | None:
    try:
        return _with_ctl(lambda: _session().ctl.read_file(path))
    except AmigaError:
        return None


@mcp.tool()
def amiga_screen_modes(action: str = "list", modes: str = "", depth: int = 0, board: str = "",
                       need: str = "", confirm: bool = False) -> str:
    """The Picasso96 screen modes and which colour depths are switched on, from
    DEVS:Picasso96Settings; switch single ones on or off.

    action: list (default, read only), on, off, or undo. modes: '640x480', a
    full name like 'VideoCore:640x480', several by comma, or 'all'; depth: 8,
    16, 24 or 32, 0 for every depth of those modes; board filters the list.
    need='640x480x32' says whether a program asking for that mode gets it and,
    if it is switched off, which switch is missing.
    on/off without confirm=True only says what would change. With it the file
    is first copied to DEVS:Picasso96Settings.amigent-undo (the very first
    state also to .amigent-orig), then changed in place. undo (confirm=True)
    puts that copy back in one call; a second undo redoes. Picasso96 reads the
    file when it starts, so a change counts after a restart. With CyberGraphX
    this only explains: its modes are not managed here.
    """
    from . import graphics
    if action not in ("list", "on", "off", "undo"):
        raise AmigaError("action: list, on, off or undo")
    ctl = _session().ctl
    data = _p96_settings(graphics.SETTINGS)
    if data is None:
        why = f"no {graphics.SETTINGS}: Picasso96 is not installed here or has not saved its modes yet"
        info = _session().graphics
        if info and info.system == "CyberGraphX":
            why += "; this Amiga runs CyberGraphX, whose modes Amigent does not manage"
        raise AmigaError(why)
    try:
        found = graphics.parse_settings(data)
        if action == "list":
            text = graphics.listing(found, board)
            return text + ("\n\n" + graphics.explain(found, need) if need else "")
        if action == "undo":
            old = _p96_settings(graphics.UNDO)
            if old is None:
                return f"nothing to undo: no {graphics.UNDO}"
            graphics.parse_settings(old)  # never put back something that is not settings
            if not confirm:
                return (f"undo would put {graphics.UNDO} back as {graphics.SETTINGS}; "
                        "call again with confirm=True")
            _with_ctl(lambda: ctl.write_file(graphics.SETTINGS, old))
            _with_ctl(lambda: ctl.write_file(graphics.UNDO, data))
            return (f"{graphics.SETTINGS} is back as it was before the last change; another undo "
                    "redoes it. Picasso96 reads it when it starts: restart the Amiga for it to count.")
        if depth not in (0, 8, 15, 16, 24, 32):
            raise AmigaError("depth: 8, 16, 24 or 32, or 0 for all")
        new, changed = graphics.change(data, modes, depth, action == "on")
    except graphics.SettingsError as e:
        raise AmigaError(str(e)) from e
    if not changed:
        return f"nothing to change: those modes are already {action}"
    what = f"switch {action}: " + ", ".join(changed)
    if not confirm:
        return f"would {what}. Call again with confirm=True (ask the user first)."
    if _p96_settings(graphics.ORIGINAL) is None:
        _with_ctl(lambda: ctl.write_file(graphics.ORIGINAL, data))
    _with_ctl(lambda: ctl.write_file(graphics.UNDO, data))
    _with_ctl(lambda: ctl.write_file(graphics.SETTINGS, new))
    if _p96_settings(graphics.SETTINGS) != new:
        _with_ctl(lambda: ctl.write_file(graphics.SETTINGS, data))
        raise AmigaError(f"{graphics.SETTINGS} did not read back as written; the old one is back")
    return (f"did {what}. The settings from before are in {graphics.UNDO} (undo: "
            "amiga_screen_modes(action='undo', confirm=True)). Picasso96 reads them when it starts: "
            "restart the Amiga (amiga_restart) for the change to count.")


@mcp.tool()
def amiga_locks(screen_id: str = "") -> str:
    """Who holds the layer locks of a screen and its windows, and who waits for them.

    For a screen that stutters or hangs: a window being dragged, a program that
    draws and holds its layer, a deadlock between two tasks. screen_id from
    amiga_screens, default the front screen. amigentd only looks (under
    Forbid), it takes no lock itself. Needs amigentd 1.4.0.
    """
    handle = _screen_handle(screen_id) if screen_id else 0
    return probes.locks(_with_ctl(lambda: _session().ctl.locks(handle)))


@mcp.tool()
def amiga_cpu(seconds: int = 5, every_ms: int = 10) -> str:
    """CPU load per task, measured on the Amiga for seconds (1-60).

    amigentd samples every every_ms milliseconds at a priority above all
    others which task was running, with no network traffic meanwhile, and
    amigent-monitor stays still. Shows percent per task and idle. Needs amigentd 1.4.0.
    Under Emu68 (amigentd 1.5.0) also the real MIPS and JIT cache misses meanwhile.
    """
    from . import emu68
    before = _emu68_counters()
    lines = _with_ctl(lambda: _session().ctl.cpu(max(1, min(seconds, 60)), max(2, min(every_ms, 1000))))
    rate = emu68.mips(before, _emu68_counters()) if before else None
    try:
        names = {t.address: t.label for t in _tasks()}
    except AmigaError:
        names = {}
    text = probes.cpu(lines, names)
    return text + ("\n" + emu68.mips_line(rate) if rate else "")


def _emu68_counters() -> dict | None:
    """Emu68's counters when this Amiga runs on it and amigentd can tell; else None."""
    from . import emu68
    try:
        if _with_ctl(_session().ctl.sysinfo).get("emu68") != "1":
            return None
        info = _emu68_info(counters=True)
    except (AmigaError, AttributeError):
        return None
    return info if emu68.present(info) else None


# ---- Emu68 on a Raspberry Pi (PiStorm and others)

def _emu68_info(counters: bool = False) -> dict:
    """EMU68's answer as a dict; {'emu68': '0'} without Emu68."""
    from . import emu68
    try:
        return emu68.parse(_with_ctl(lambda: _session().ctl.emu68_info(counters)))
    except AttributeError:  # amigactld
        raise AmigaError("asking Emu68 needs amigentd 1.5.0 or newer; run amigent-setup --update") from None


def _emu68_extras() -> list[str]:
    """PiStormBoot and Emu68PWM, when they are there."""
    out = []
    try:
        loaded = _loaded()
    except AmigaError:
        loaded = None
    if loaded is not None:
        for r in loaded.residents:
            if "pistormboot" in r.name.lower():
                out.append(f"PiStormBoot {r.version} is loaded: it applies its own JIT settings again at "
                           "every start, so values seen after a restart come from it")
                break
        for lib in loaded.libraries:
            if lib.name.lower() == "emu68pwm.audio":
                out.append(f"Emu68PWM {lib.version}.{lib.revision} plays through the Pi's audio jack "
                           "(a second sound path next to Paula or a sound card)")
                break
        else:
            try:
                _with_ctl(lambda: _session().ctl.stat("DEVS:AHI/emu68pwm.audio"))
                out.append("Emu68PWM is installed (DEVS:AHI/emu68pwm.audio), an AHI driver for the Pi's audio jack")
            except AmigaError:
                pass
    return out


@mcp.tool()
def amiga_emu68() -> str:
    """What runs on the Raspberry Pi under Emu68 (PiStorm and other Pi boards), read only.

    Emu68 version and variant, the Pi's model and memory, the boot options from
    cmdline.txt, its health (temperature, ARM clock, core voltage, and whether
    the Pi reported under-voltage or throttling since power on), the JIT
    cache and counters, and the JIT settings now. Names PiStormBoot and
    Emu68PWM when they are there. On an Amiga without Emu68 it says
    "not Emu68" and touches nothing. Changes nothing. Needs amigentd 1.5.0.
    """
    from . import emu68
    info = _emu68_info()
    if not emu68.present(info):
        return emu68.describe(info)
    return emu68.describe(info, _emu68_extras())


def _emu68_sysinfo() -> dict:
    return _with_ctl(_session().ctl.sysinfo)


@mcp.tool()
def amiga_emu68_jit(settings: str = "", restore: bool = False, program: str = "",
                    wait: float = 600, remember: bool = False) -> str:
    """Show or change Emu68's JIT settings while the Amiga runs, with no restart.

    Without arguments: the settings now, what each does, and what was saved
    before a change. settings like "chip_slowdown=1 dbf_slowdown=1" changes
    them at once (EmuControl's short names work too: sc, scs, dbf, bw, ccrd,
    icnt, irng, lcnt, sf, sfl); the values from before the first change are
    kept on this PC, and restore=True puts them back. Only the settings Emu68
    documents as changeable; other bits stay. Nothing is written to the SD
    card: a restart brings back what cmdline.txt (or PiStormBoot) sets.

    program: start it as a job with these settings (or the ones noted for it
    earlier) and put the old ones back on the Amiga when it ends; wait as in
    amiga_job. remember=True keeps the settings as an amiga_note for the
    program, so the next start uses them. Needs amigentd 1.5.0 on Emu68.
    """
    from . import emu68
    info = _emu68_info(counters=True)
    if not emu68.present(info):
        return emu68.describe(info)
    current = emu68.settings(info)
    saved = emu68.saved_for(_amiga())
    booted = emu68.booted_at(_emu68_sysinfo())
    before = saved.load(booted)
    try:
        changes = emu68.requested(_settings_words(settings))
        if changes:
            emu68.check(current, changes)
    except ValueError as e:
        raise AmigaError(str(e)) from None
    if restore:
        if program or changes:
            raise AmigaError("restore=True goes alone")
        if before is None:
            return "nothing to take back: no change was made since the Amiga started"
        after = emu68.parse(_with_ctl(lambda: _session().ctl.emu68_set(emu68.restore_fields(before))))
        saved.clear()
        return "put back: " + _jit_line(emu68.settings(after))
    if program:
        return _emu68_program(program, changes, current, info, wait, remember)
    if not changes:
        lines = [f"{s.name}={current[s.name]}  ({s.text}; {s.low}-{s.high}, default {s.default})"
                 for s in emu68.SETTINGS]
        if before is not None:
            lines.append("before the first change: " + _jit_line(_decoded(before)) +
                         "; restore=True puts that back")
        return "\n".join(lines)
    if before is None:
        saved.keep(emu68.registers(info), booted)
    after = emu68.parse(_with_ctl(lambda: _session().ctl.emu68_set(emu68.fields(changes))))
    now = emu68.settings(after)
    wrong = {k: v for k, v in changes.items() if now.get(k) != v}
    text = "set: " + " ".join(f"{k}={now[k]}" for k in changes)
    if wrong:
        text += "\nnot taken as asked: " + ", ".join(f"{k} is {now.get(k)}" for k in wrong)
    if remember:
        text += "\n" + (_emu68_remember(_session().last_started, changes) if _session().last_started
                        else "remember=True needs a program: none was started in this session")
    return text + "\nrestore=True takes it back; a restart does as well"


def _settings_words(text: str) -> dict:
    out = {}
    for word in text.replace(",", " ").split():
        if "=" not in word:
            raise ValueError(f"{word!r}: settings go as name=value")
        k, v = word.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _decoded(regs: dict[int, int]) -> dict[str, int]:
    from . import emu68
    return {s.name: s.decode(regs) for s in emu68.SETTINGS}


def _jit_line(values: dict[str, int]) -> str:
    return " ".join(f"{k}={v}" for k, v in values.items())


def _emu68_remember(program: str, changes: dict[str, int]) -> str:
    from . import emu68
    topic = notes.topic_of(program)
    if not topic:
        return ""
    book = notes.for_amiga(_amiga())
    for old in book.get(topic):
        if old.startswith("Emu68 JIT:"):
            book.remove(topic, old)
    book.add(topic, emu68.note_text(changes))
    return f"noted for {topic!r}: {emu68.note_text(changes)}"


def _emu68_program(program: str, changes: dict[str, int], current: dict[str, int], info: dict,
                   wait: float, remember: bool) -> str:
    """The program as a job between two 'amigentd EMU68JIT' lines: the Amiga puts the
    settings back itself when it ends, also if this PC is gone meanwhile."""
    from . import emu68
    said = ""
    if not changes:
        changes = emu68.from_notes(notes.for_amiga(_amiga()).get(notes.topic_of(program)))
        if not changes:
            raise AmigaError(f"no settings given and none noted for {notes.topic_of(program)!r}")
        said = "settings from the note: "
    try:
        emu68.check(current, changes)
    except ValueError as e:
        raise AmigaError(str(e)) from None
    me = _emu68_sysinfo().get("self", "")
    if not me:
        raise AmigaError("amigentd does not say where it lives (self in SYSINFO), so the job "
                         "cannot set and restore the JIT settings itself")
    regs = emu68.registers(info)
    saved = emu68.saved_for(_amiga())
    booted = emu68.booted_at(_emu68_sysinfo())
    if saved.load(booted) is None:  # restore=True still works should the script be broken off
        saved.keep(regs, booted)
    back = emu68.restore_fields(regs)
    # FailAt: the line that puts the settings back runs whatever the program returns
    command = "\n".join(["FailAt 2147483647", f'"{me}" EMU68JIT {emu68.cli_spec(emu68.fields(changes))}',
                         program, f'"{me}" EMU68JIT {emu68.cli_spec(back)}'])
    number = _start_job(command, title=f"{program} (Emu68 JIT: {_jit_line(changes)})")
    _session().last_started = program
    text = f"{said}{_jit_line(changes)} while {program} runs (job {number}); the old ones come back when it ends"
    if remember:
        text += "\n" + _emu68_remember(program, changes)
    result = _wait_job(number, wait)
    if result is None:
        return text + f"\njob {number} still running; amiga_job_output({number}) shows what it printed"
    return text + "\n" + (result or "(no output)")


@mcp.tool()
def amiga_quiet_monitor(seconds: int = 30) -> str:
    """amigent-monitor's window on the Amiga stops drawing for seconds (0: draws again).

    Its drawing holds its window's layer, and Intuition waits for that: it can
    spoil a measurement of how smoothly windows move. amiga_cpu and
    amiga_drag_measure do this themselves. Needs amigentd 1.4.0.
    """
    _with_ctl(lambda: _session().ctl.quiet(max(0, min(seconds, 3600))))
    return f"amigent-monitor quiet for {seconds} s" if seconds else "amigent-monitor draws again"


@mcp.tool()
def amiga_drag_measure(window: str, pattern: str = "zigzag", steps: int = 64, step: int = 8) -> str:
    """Measure how smoothly a window follows when it is dragged by its title bar.

    amigentd drags it itself, at priority 21 above input.device: zigzag (8
    steps one way, 8 back) or jumps (4 steps' worth each way in turn), steps
    of step pixels, and records each frame: time per step, how long
    input.device took the event, where the window and the pointer are. Shows
    median, p95 and max ms per step and the steps the window did not move.
    window is part of its title or its id from amiga_ui_tree; it must be on
    the front screen with its title bar uncovered. It ends where it started.
    Needs amigentd 1.4.0.
    """
    kinds = {"zigzag": 0, "jumps": 1}
    if pattern not in kinds:
        raise AmigaError("pattern: zigzag or jumps")
    w = _window(window)
    steps = max(16, min(steps, 512)) // 16 * 16  # whole zigzags: back where it started
    lines = _with_ctl(lambda: _session().ctl.drag_measure(int(w["id"], 16), kinds[pattern], steps, max(1, min(step, 64))))
    return f"window {w['title']!r} {w['id']}, {pattern}\n" + probes.drag(lines, step)


def _window(which: str) -> dict:
    """A window of the front screen by id or part of its title, the frontmost if several."""
    wins = [i for i in _with_screen(_session().screen.ui_tree) if i["kind"] == "window"]
    hits = [w for w in wins if which.lower() in (w["id"].lower(), w["id"].lower().lstrip("0"))]
    hits = hits or [w for w in wins if which.lower() in w["title"].lower()]
    if not hits:
        raise AmigaError(f"no window on the front screen matches {which!r}; amiga_ui_tree lists them")
    return hits[0]


@mcp.tool()
def amiga_memory_write(address: str, hex_bytes: str) -> str:
    """Write bytes into the Amiga's RAM (not ROM, not chips), e.g. to patch a test
    program. Off unless amigentd runs with ALLOWPOKE (command line or tool type):
    a wrong byte can crash the Amiga. hex_bytes like '4e71 4e71'. Needs amigentd 1.4.0."""
    data = bytes.fromhex(hex_bytes.replace(" ", "").replace("$", ""))
    namer = _namer()
    try:
        at = memory.resolve(address, namer)
    except ValueError as e:
        raise AmigaError(str(e)) from None
    before = _with_ctl(lambda: _session().ctl.peek(at, len(data)))
    _with_ctl(lambda: _session().ctl.poke(at, data))
    return f"wrote {len(data)} bytes at ${at:08X}; before: {before.hex()}"


@mcp.tool()
def amiga_trace(action: str = "list", address: str = "") -> str:
    """Tracepoints in a running program: where it gets to, and its registers then.

    action set: put one at address (in a running program's code, e.g. from
    amiga_loaded or 'myprog hunk 0 +$120' resolved with amiga_where); when the
    program passes there, amigentd keeps D0-A7, SR and the top of the stack, puts
    the instruction back and the program runs on without stopping. Fires once.
    action list shows them with what they caught; clear removes all. At most 8.
    Meant for programs you are testing. Needs amigentd 1.4.0.
    """
    if action == "set":
        namer = _namer()
        try:
            at = memory.resolve(address, namer)
        except ValueError as e:
            raise AmigaError(str(e)) from None
        _with_ctl(lambda: _session().ctl.trace(1, at))
        where = namer.name(at, search_functions=False)
        return f"tracepoint at ${at:08X}" + (f" ({where})" if where else "") + "; amiga_trace() shows when it fired"
    if action == "clear":
        _with_ctl(lambda: _session().ctl.trace(3))
        return "tracepoints cleared"
    if action == "list":
        lines = _with_ctl(lambda: _session().ctl.trace(2))
        try:
            namer = _namer()
        except AmigaError:
            namer = None
        return memory.describe_traces(lines, namer)
    raise AmigaError("action: set, list or clear")


@mcp.tool()
def amiga_state(action: str = "save", name: str = "before") -> str:
    """Find leaks: save the Amiga's state, run and quit a program, then compare.

    save keeps free memory and the largest blocks, the open count of every
    library and device (with amigentd 1.4.0), the tasks and the public ports;
    compare says what changed since: memory not given back, a library left
    open, a task or port that stayed.
    """
    from . import states
    store = states.Store(secret.FOLDER / "states")
    info = _with_ctl(_session().ctl.sysinfo)
    try:
        loaded, tasks = _loaded(), _tasks()
    except AmigaError:
        loaded = tasks = None  # before 1.4.0: memory, tasks and ports from SYSINFO only
    now = states.take(info, loaded, tasks)
    if action == "save":
        path = store.save(_amiga_name(), name, now)
        mem = now["memory"]
        return (f"saved as {name!r}: {mem.get('chip_free', 0) // 1024} KB Chip and "
                f"{mem.get('fast_free', 0) // 1024} KB Fast free, {len(now['tasks'])} tasks "
                f"({path}); amiga_state('compare', {name!r}) afterwards")
    if action == "compare":
        before = store.load(_amiga_name(), name)
        if not before:
            raise AmigaError(f"nothing saved as {name!r}; amiga_state('save', {name!r}) first")
        return states.compare(before, now)
    raise AmigaError("action: save or compare")


@mcp.tool()
def amiga_screen_diff(action: str = "compare", name: str = "baseline", x: int = 0, y: int = 0,
                      width: int = 0, height: int = 0, threshold: int = 24) -> list:
    """Visual regression test: save a picture of the screen (or a region), later
    compare the screen with it. compare answers the share of changed pixels, the
    boxes of the changed areas and a picture of them (changed pixels magenta,
    boxes yellow). threshold: how different a pixel must be (0-255)."""
    from io import BytesIO
    from PIL import Image
    from . import screendiff
    folder = secret.FOLDER / "screens" / journal._safe(_amiga_name())
    path = folder / f"{journal._safe(name)}.png"
    # the 256 colour picture: tiles come from the cache, so a comparison costs little
    image = _with_screen(lambda: _session().screen.capture(x, y, width or None, height or None)).convert("RGB")
    if action == "save":
        folder.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return [f"saved {image.size[0]}x{image.size[1]} as {name!r} ({path})"]
    if action != "compare":
        raise AmigaError("action: save or compare")
    if not path.exists():
        raise AmigaError(f"nothing saved as {name!r}; amiga_screen_diff('save', {name!r}) first")
    report, picture = screendiff.compare(Image.open(path), image, threshold)
    buf = BytesIO()
    small, _ = pictures.fit(picture)
    small.save(buf, "PNG")
    return [json.dumps(report), McpImage(data=buf.getvalue(), format="png")]


def _devpack(given: str) -> str:
    if given:
        return given
    entry = (machines.find(_session().host) or {}) if _session().host else {}
    if entry.get("devpack"):
        return entry["devpack"]
    for guess in ("SYS:DevPack", "Work:DevPack", "Stuff:DevPack"):
        if _with_ctl(lambda: _session().ctl.exec(f'List >NIL: "{guess}/vbcc/bin/vc"')).rc == 0:
            return guess
    return "SYS:DevPack"


@mcp.tool()
def amiga_create_project(name: str, folder: str = "", kind: str = "shell", cpu: str = "68000",
                         devpack: str = "") -> str:
    """Start a new C program on the Amiga: main.c (kind shell or window), a build
    script for vbcc and a ReadMe, in folder/name (default: Amigent's folder,
    projects/). Then amiga_build (target="warpos" for a PowerPC program
    under WarpOS), then amiga_run. devpack is the folder with
    vbcc/ and SDK/NDK 3.9/ (found by itself in SYS:, Work: or Stuff:, or set as
    "devpack" in amigas.json)."""
    from . import project
    try:
        made = project.files(name, kind, cpu, _devpack(devpack), time.strftime("%-d.%-m.%Y"))
    except ValueError as e:
        raise AmigaError(str(e)) from None
    target = _join(folder or _join(_home(), "projects"), name)
    _with_ctl(lambda: files.makedirs(_session().ctl, target))
    for file, text in made.items():
        _with_ctl(lambda: _session().ctl.write_file(_join(target, file), text.encode("iso-8859-1")))
    return (f"created {target}: {', '.join(made)}; amiga_build({target!r}) compiles it, "
            f"amiga_run({_join(target, name)!r}) runs it")


FPU_BUILDS = {"68881": "68881", "68882": "68881", "68040": "68040", "68060": "68040"}


def _build_fpu(fpu: str, cpu: str) -> tuple[str, str, str]:
    """(FPU argument for the build script, CPU, a note) for amiga_build's fpu option.

    auto takes what this Amiga has: m881.lib for a 68881/68882, m040.lib for a
    68040/060 FPU. A machine without an FPU is refused; an unknown one is warned of.
    """
    want = fpu.strip().lower()
    have = _fpu()
    if want in ("auto", "yes", "true", "on", "1"):
        if have is None or have.present is None:
            raise AmigaError("fpu=auto: this Amiga's FPU is not known (amigentd does not report "
                             "AttnFlags); give fpu='68881' or fpu='68040' to build for one anyway")
        want = "68040" if have.kind == "internal" and have.library else "68881"
    if want not in FPU_BUILDS:
        raise AmigaError("fpu: '' (no FPU code, runs everywhere), auto, 68881 or 68040")
    want = FPU_BUILDS[want]
    if have is not None and have.present is False:
        raise AmigaError(f"not building FPU code: {have.line()}, so the program would crash with "
                         "a line-F guru here. Build without fpu, or give an Amiga with an FPU as machine")
    if cpu and cpu.strip().upper().lstrip("MC") in ("68000", "68010"):
        raise AmigaError(f"FPU code needs a 68020 or better, not cpu {cpu}")
    cpu = cpu or ("68040" if want == "68040" else "68020")
    lib = "m040.lib" if want == "68040" else "m881.lib"
    note = (f"FPU code (-fpu={want}, {lib}): it runs only on an Amiga with an FPU"
            + (" (a 68040 or 68060 one)" if want == "68040" else ""))
    if have is None or have.present is None:
        note += "; this Amiga's FPU is not known, so it was not checked"
    elif want == "68040" and have.kind != "internal":
        note += f"; this Amiga has a {have.kind}, which m040.lib is not for: fpu='68881' fits it"
    elif have.support_missing:
        note += (f"; {have.library} is not loaded here, so instructions the chip lacks will crash"
                 + ("" if want == "68040" else ": fpu='68040' avoids most of them"))
    return want, cpu, note


BUILD_TARGETS = {"": "", "68k": "", "m68k": "", "aos68k": "", "warpos": "WARPOS", "wos": "WARPOS"}


@mcp.tool()
def amiga_build(folder: str, cpu: str = "", devpack: str = "", fpu: str = "", target: str = "") -> str:
    """Compile a project on the Amiga by running its build script (as a job), and
    report the compiler's messages and whether it worked.

    fpu: '' (default) builds without FPU instructions, so the program runs on
    every Amiga. 'auto' builds FPU code for the FPU this Amiga has, '68881'
    (m881.lib, 68881/68882 or a 68040/060 with its library) or '68040'
    (m040.lib, a 68040/060 FPU). Refused on an Amiga without an FPU. Projects
    made before Amigent 1.5.0 need a new build script for it (amiga_create_project).

    target: '' builds 68k code; 'warpos' a PowerPC program for WarpOS with
    vbcc's ppc-warpos target (+warpos), which must be installed in vbcc on the
    Amiga; the script says so if it is not. Not tried on a real PowerPC board yet.
    """
    note = ""
    want = target.strip().lower()
    if want not in BUILD_TARGETS:
        raise AmigaError("target: '' (68k) or 'warpos'")
    target = BUILD_TARGETS[want]
    if target and (fpu or cpu):
        raise AmigaError("fpu and cpu are for 68k builds; a WarpOS build is PowerPC code, which "
                         "always uses the PowerPC's own FPU")
    if fpu or target:
        try:
            script = _with_ctl(lambda: _session().ctl.read_file(_join(folder, "build"))).decode("iso-8859-1")
        except AmigaError as e:
            raise AmigaError(f"no build script in {folder}: {e}") from None
        option, what = ("TARGET/K", "WarpOS target") if target else ("FPU/K", "FPU option")
        if option not in script.split("\n", 1)[0]:
            raise AmigaError(f"{_join(folder, 'build')} has no {what} yet; a project from "
                             "amiga_create_project of Amigent 1.5.0 or newer has it (copy its "
                             "build script over, keeping main.c)")
    if fpu:
        fpu, cpu, note = _build_fpu(fpu, cpu)
    if target:
        note = "PowerPC code for WarpOS (vbcc +warpos)"
        try:
            on_disk = _system_facts().get("ppc", "")
        except AmigaError:
            on_disk = None
        if on_disk is not None and "WarpOS" not in on_disk:
            note += "; this Amiga has no powerpc.library in LIBS:, so it cannot run it"
    args = ((f" CPU {cpu}" if cpu else "") + (f' DEVPACK "{devpack}"' if devpack else "")
            + (f" FPU {fpu}" if fpu else "") + (f" TARGET {target}" if target else ""))
    out = amiga_job(f'CD "{folder}"\nExecute build{args}', wait=900)
    ok = "built " in out and "build failed" not in out
    return ("built" if ok else "build failed") + (f" with {note}" if note else "") + "\n" + out


@mcp.tool()
def amiga_everywhere(command: str, timeout: float = 120) -> str:
    """Run one AmigaDOS command on every Amiga Amigent knows (amigas.json), at once,
    and compare: which agree, which differ, with each one's output. For checks
    across machines, such as a version or a test on the A500 and the A1200."""
    from . import fleet
    amigas = machines.load()
    if len(amigas) < 1:
        raise AmigaError("no Amigas known; amigent-fleet --add NAME ADDRESS")

    def execute(target, command, timeout):
        session, _ = sessions.resolve(target)
        with session.calls, bound(session):
            session.refresh_address()
            return _with_ctl(lambda: session.ctl.exec(command, timeout=timeout))

    return fleet.compare_runs(fleet.run_everywhere(command, amigas, timeout, execute=execute))


GURU_LOGS = ("S:GuruHistory", "S:Guru.log", "ENVARC:GuruHistory")


@mcp.tool()
def amiga_gurus(code: str = "") -> str:
    """Guru meditations in words: the code given, the last one exec kept, and the
    gurus logged by tools such as MCP (S:GuruHistory), explained."""
    from . import alerts
    if code:
        c, _, where = code.strip().lstrip("#").partition(".")
        hint = _fpu_hint(c)
        return f"{code}: {alerts.describe(c, where or None)}" + (f"\n{hint}" if hint else "")
    extra = {}
    try:
        extra = alerts.gurudat(_with_ctl(lambda: _session().ctl.read_file("S:MCP.gurudat")).decode("iso-8859-1"))
    except AmigaError:
        pass
    out = []
    info = _with_ctl(_session().ctl.sysinfo)
    if info.get("last_alert"):
        c, w = info["last_alert"], info.get("last_alert_where", "")
        out.append(f"last alert exec kept: {c.upper()}{'.' + w.upper() if w else ''} -> "
                   f"{alerts.describe(c, w or None)}")
        hints = [c]
    else:
        out.append("exec kept no alert from before the last reset")
        hints = []
    for path in GURU_LOGS:
        try:
            text = _with_ctl(lambda: _session().ctl.read_file(path)).decode("iso-8859-1")
        except AmigaError:
            continue
        found = alerts.history(text, extra)
        out.append(f"{path}: " + (f"{len(found)} guru(s)" if found else "no gurus logged"))
        out += [f"  {x}" for x in found[-20:]]
        hints += alerts.codes(text)[-20:]
    said = set()
    for c in hints:  # each kind of FPU guru explained once
        hint = _fpu_hint(c)
        if hint and hint not in said:
            said.add(hint)
            out.append(f"FPU: {hint}")
    return "\n".join(out)




def _emulator_endpoint():
    entry = machines.find(_session().host) or {}
    if entry.get("gdb"):
        return entry["gdb"]
    if _process_target():
        return os.environ.get("AMIGENT_GDB")


def _emulator() -> "gdb.Gdb":
    where = _emulator_endpoint()
    if not where:
        raise AmigaError("no emulator debugger set: for an Amiga in WinUAE 6.1 on this PC put "
                         '"gdb": "127.0.0.1:2345" into its entry in amigas.json (or AMIGENT_GDB), '
                         "and start WinUAE with -s debugging_features=gdbserver -s gdb_port=2345 "
                         "-s cpu_threaded=false")
    if _session().gdb.get("where") != where:
        if _session().gdb.get("conn"):
            _session().gdb["conn"].close()
        _session().gdb.update(where=where, conn=gdb.Gdb(where))
    return _session().gdb["conn"]


def _checkpoint_folder() -> "Path":
    from pathlib import Path
    return Path(secret.FOLDER) / "checkpoints" / journal._safe(_amiga_name())


def _winuae() -> str:
    entry = (machines.find(_session().host) or {}) if _session().host else {}
    exe = entry.get("winuae") or os.environ.get("AMIGENT_WINUAE")
    if exe:
        return exe
    for guess in ("/mnt/c/Program Files/WinUAE/winuae64.exe", "C:\\Program Files\\WinUAE\\winuae64.exe",
                  "/Applications/WinUAE.app/Contents/MacOS/WinUAE"):
        if os.path.exists(guess):
            return guess
    raise AmigaError('no WinUAE found; set "winuae" (the program) in amigas.json or AMIGENT_WINUAE')


def _launch_emulator(config: str, disk: str = "") -> str:
    import subprocess
    old = _session().gdb.get("proc")
    if old and old.poll() is None:
        raise AmigaError(f"the emulator Amigent started runs already (pid {old.pid}); quit it first")
    where = _emulator_endpoint()
    if not where:
        if not _process_target():
            raise AmigaError("Set a gdb endpoint for this machine in amigas.json before launching WinUAE.")
        where = "127.0.0.1:2345"
    port = gdb.parse_address(where)[1]
    config = config or ((machines.find(_session().host) or {}).get("winuae_config") if _session().host else "") or ""
    if not config:
        raise AmigaError('which configuration? config= a .uae file, or "winuae_config" in amigas.json')
    args = [_winuae(), "-f", _host_path(config), "-s", "use_gui=no", "-s",
            "debugging_features=gdbserver", "-s", f"gdb_port={port}", "-s", "cpu_threaded=false"]
    if disk:
        args += ["-s", f"floppy0={_host_path(disk)}"]
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    _session().gdb.update(proc=proc, config=config)
    host = gdb.parse_address(where)[0]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AmigaError(f"WinUAE ended at once (exit {proc.returncode}); WinUAE 6.1 or newer "
                             "has the GDB server, and a new beta wants its first start by hand")
        if others.listening(host, port, 0.5):
            return f"WinUAE started (pid {proc.pid}), GDB server on {where}"
        time.sleep(1)
    return f"WinUAE started (pid {proc.pid}), but no GDB server on {where} after 60 s"


@mcp.tool()
def amiga_emulator(action: str = "status", file: str = "", address: str = "", size: int = 256,
                   config: str = "", disk: str = "", show: str = "all") -> str:
    """The emulated Amiga from outside, through WinUAE's GDB server (WinUAE 6.1+).

    action: status (running or stopped, and why), pause, resume, registers (with
    the PC named), memory (address and size, read by the emulator, even when
    the Amiga hangs), save or restore (the whole machine as a state file; file
    is a path on this PC, by default in Amigent's checkpoints folder), or
    postmortem (registers, the code at the PC and the stack, for a hang or a
    guru), chipset (show: all, paula, copper, sprites, dma or registers; the
    custom chips as the emulator keeps them). launch starts WinUAE with config
    (a .uae file) and its GDB server, disk puts an .adf into DF0:; quit ends
    the WinUAE Amigent started, never another one; insert_disk restarts it
    with disk in DF0:. Needs "gdb" in the Amiga's amigas.json entry.
    """
    if action == "launch":
        return _launch_emulator(config, disk)
    if action in ("quit", "insert_disk"):
        proc = _session().gdb.get("proc")
        if not proc or proc.poll() is not None:
            raise AmigaError("no WinUAE started by Amigent runs; an emulator started otherwise "
                             "is left alone")
        if _session().gdb.get("conn"):
            _session().gdb["conn"].close()
            _session().gdb.pop("where", None)
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:  # noqa: BLE001
            proc.kill()
        if action == "quit":
            return f"WinUAE (pid {proc.pid}) ended"
        if not disk:
            raise AmigaError("insert_disk needs disk= an .adf on this PC")
        return _launch_emulator(_session().gdb.get("config", ""), disk)
    emu = _emulator()
    try:
        if action == "status":
            caps = ""
            try:
                caps = emu.monitor("execution-status")
            except gdb.GdbError:
                pass
            return ("running" if emu.running else f"stopped ({emu.why()})") + (f"\n{caps}" if caps else "")
        if action == "pause":
            return f"paused: {emu.pause()}"
        if action == "resume":
            emu.resume()
            return "running"
        if action == "registers":
            regs = emu.registers()
            try:
                namer = _namer()
            except Exception:  # noqa: BLE001 - a hung Amiga can't name anything
                namer = None
            return gdb.describe_registers(regs, namer)
        if action == "memory":
            from . import memory as mem
            at = int(address.lstrip("$"), 16) if address else 0
            return mem.hexdump(emu.read(at, max(1, min(size, 65536))), at)
        if action in ("save", "restore"):
            path = file or str(_checkpoint_folder() / "manual.uss")
            if action == "save":
                from pathlib import Path
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            said = emu.checkpoint(action, _host_path(path))
            return f"{action}d {path}: {said}"
        if action == "chipset":
            from . import chipset
            regs = emu.read(0xDFF000, 0x200)
            parts = {"dma": chipset.dma(regs), "paula": chipset.paula(regs),
                     "sprites": chipset.sprites(regs), "registers": chipset.registers(regs)}
            if show in ("all", "copper"):
                start = chipset.long(regs, 0x080)
                parts["copper"] = f"copper list 1 at ${start:06X}:\n" + chipset.copper(
                    emu.read(start, max(16, min(size, 4096))), start)
            if show == "all":
                return "\n\n".join(parts[k] for k in ("dma", "paula", "copper", "sprites"))
            if show not in parts:
                raise AmigaError("show: all, paula, copper, sprites, dma or registers")
            return parts[show]
        if action == "postmortem":
            was = emu.running
            if was:
                emu.pause()
            try:
                regs = emu.registers()
                code = emu.read(max(regs["pc"] - 16, 0), 64)
                stack = emu.read(regs["a7"], 64)
            finally:
                if was:
                    emu.resume()
            from . import memory as mem
            return "\n".join([gdb.describe_registers(regs), "code around the PC:",
                              mem.disassemble(code, max(regs["pc"] - 16, 0)),
                              "stack:", mem.hexdump(stack, regs["a7"])])
    except gdb.GdbError as e:
        raise AmigaError(str(e)) from None
    except OSError as e:
        emu.close()
        raise AmigaError(f"the emulator's GDB server stopped answering: {e}") from None
    raise AmigaError("action: status, pause, resume, registers, memory, save, restore, postmortem, "
                     "chipset, launch, quit or insert_disk")


def _host_path(path: str) -> str:
    """WinUAE on Windows wants a Windows path; from WSL /mnt/c/... becomes C:\\..."""
    import re as _re
    m = _re.match(r"^/mnt/([a-z])/(.*)$", path)
    if m:
        return f"{m.group(1).upper()}:\\" + m.group(2).replace("/", "\\")
    if path.startswith("/") and os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop"):
        try:
            import subprocess
            return subprocess.run(["wslpath", "-w", path], capture_output=True, text=True,
                                  timeout=5).stdout.strip() or path
        except (OSError, subprocess.SubprocessError):
            return path
    return path


@mcp.tool()
def amiga_list(path: str, recursive: bool = False) -> str:
    """List a directory, e.g. 'Work:Games'. Amiga paths use VOLUME:dir/file."""
    entries = _with_ctl(lambda: _session().ctl.list_dir(path, recursive))
    return "\n".join(
        f"{e['type']:4} {e['size']:>10} {e['date']} {e['name']}" for e in entries
    )


@mcp.tool()
def amiga_read_text(path: str) -> str:
    """Read a text file from the Amiga."""
    return _with_ctl(lambda: _session().ctl.read_file(path)).decode("iso-8859-1")


@mcp.tool()
def amiga_write_text(path: str, text: str) -> str:
    """Create or replace a text file on the Amiga, with LF line endings."""
    _with_ctl(lambda: _session().ctl.write_file(path, text.encode("iso-8859-1")))
    return f"wrote {path}"


@mcp.tool()
def amiga_put(local_path: str, amiga_path: str) -> str:
    """Copy a local file or directory tree to the Amiga.

    amiga_path is the full target name, e.g. 'Work:Games/Foo'. Files the Amiga
    already has with the same size and CRC are left alone, so putting a tree
    again only sends what changed. While an installation is open
    (amiga_install_begin), recorded files it is about to overwrite are copied
    aside first, so undo can bring them back.
    """
    src = Path(local_path).expanduser()
    guarded = ""
    if _installs_open():
        if src.is_dir():
            targets = [_join(amiga_path, p.relative_to(src).as_posix()) for p in src.rglob("*") if p.is_file()]
        else:
            targets = [amiga_path]
        guarded = _install_guard(targets)
    said = _with_ctl(lambda: files.put(_session().ctl, src, amiga_path))
    return f"{said}; {guarded}" if guarded else said


@mcp.tool()
def amiga_get(amiga_path: str, local_path: str) -> str:
    """Copy a file or a whole directory tree from the Amiga to the local machine.

    Local files with the same size and CRC are not fetched again.
    """
    return _with_ctl(lambda: files.get(_session().ctl, amiga_path, Path(local_path).expanduser()))


@mcp.tool()
def amiga_mkdir(path: str) -> str:
    """Create a directory, including missing parents."""
    _with_ctl(lambda: files.makedirs(_session().ctl, path))
    return f"created {path}"


@mcp.tool()
def amiga_move(source: str, dest: str) -> str:
    """Rename or move a file within one volume."""
    _with_ctl(lambda: _session().ctl.rename(source, dest))
    return f"moved {source} to {dest}"


@mcp.tool()
def amiga_delete(path: str) -> str:
    """Delete one file or one empty directory. There is no undo."""
    _with_ctl(lambda: _session().ctl.delete(path))
    return f"deleted {path}"


@mcp.tool()
def amiga_inventory(max_wait: float = 1800) -> str:
    """Scan installed programs and versions on the Amiga; can take many minutes.

    Saves JSON under inventory/ and reports differences to the previous scan.
    """
    _with_ctl(lambda: _session().ctl.exec(f"Run >NIL: Execute {AMIGA_DIR}/amigent-scan"))
    time.sleep(5)  # let the script remove the previous marker
    deadline = time.monotonic() + max_wait
    while True:
        try:
            _with_ctl(lambda: _session().ctl.stat("RAM:amigent-scan.done"))
            break
        except AmigaError:
            if time.monotonic() > deadline:
                raise AmigaError("scan did not finish in time") from None
            time.sleep(10)
    text = _with_ctl(lambda: _session().ctl.read_file("RAM:amigent-inventory.txt")).decode("iso-8859-1")
    items = inventory.parse(text)
    folder = inventory_folder()
    folder.mkdir(parents=True, exist_ok=True)
    scans = sorted(folder.glob("*.json"))
    previous = json.loads(scans[-1].read_text(encoding="utf-8")) if scans else None
    target = folder / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    target.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
    summary = {"file": str(target), "files": len(items),
               "with_version": sum(1 for i in items.values() if i["version"])}
    try:
        processor = _system_facts().get("processor")
    except AmigaError:
        processor = ""
    if processor:
        summary["processor"] = processor
    if previous is not None:
        summary["diff"] = inventory.diff(previous, items)
    return json.dumps(summary, indent=1, ensure_ascii=False)


@mcp.tool()
def amiga_screenshot(x: int = 0, y: int = 0, width: int = 0, height: int = 0,
                     changed: bool = False, text: bool = False, screen: str = "") -> list:
    """Capture the Amiga screen, or a region of it to read small text.

    Transfers only what changed since the last call, so repeated shots are
    quick. Colours are reduced to 256. All coordinates for the pointer tools
    are screen pixels; use the scale in the reply for reduced images. If the
    same region has not changed since your last shot of it, only a note comes
    back; ask again right away to get the picture anyway. changed=True shows
    only the part that changed since your last look (after a click, say), with
    its place on the screen. text=True adds the recorded text of the region,
    see amiga_screen_text; for information alone that one is far cheaper.
    The picture is the frontmost Intuition screen, graphics card or chipset;
    screen= takes a handle or part of a title from amiga_screens to picture
    one behind it. The reply says when the display shows something else.
    """
    if changed:
        out = changed_shot()
        return out + ([amiga_screen_text()] if text else [])
    if paused := _paused_note():
        return [paused]
    found = None
    if screen:
        found = _with_screen(_session().screen.screens)
        target = _pick_screen(found, screen)
        if not target["front"]:
            return _shot_behind(target, found, (x, y, width, height), text)
    region = (x, y, width, height)
    image = _with_screen(lambda: _session().screen.capture(x, y, width or None, height or None))
    seen, _session().last_look = _session().last_look, time.monotonic()  # after the fetch: what it brought is what was seen
    note = _same_shot(region, seen)
    _session().last_shot = (region, _session().last_look, bool(note))
    if note:
        return [note]
    size = image.size
    image, scale = pictures.fit(image)
    note = (
        f"screen {_session().screen.width}x{_session().screen.height}; region {size[0]}x{size[1]} at ({x},{y}); "
        f"screen_x = {x} + image_x * {scale:.3f}, "
        f"screen_y = {y} + image_y * {scale:.3f}"
    )
    if found is None:
        found = _screens_or_none()  # after the picture: the link is up, the protocol known
    if shown := display_note(found or []):
        note += "\n" + shown
    if slow := _slow_bus_note(region):
        note += "\n" + slow
    out = [note, McpImage(data=pictures.png(image), format="png")]
    if text:
        try:
            out.append(amiga_screen_text(x=x, y=y, width=width, height=height))
        except AmigaError as e:
            out.append(f"no text: {e}")
    return out


def _screens_or_none() -> list | None:
    """The screens as SCREENS lists them, or None where amigentd can't say."""
    screen = _session().screen
    if not hasattr(screen, "screens"):
        return None
    try:
        return _with_screen(screen.screens)
    except (AmigaError, OSError, AmigentdError):
        return None


def _pick_screen(found: list, screen_id: str) -> dict:
    """A screen by handle or part of its title, as _find_screen, from a list in hand."""
    try:
        handle = int(screen_id, 0)
        return next(s for s in found if s["handle"] == handle)
    except (ValueError, StopIteration):
        matches = [s for s in found if screen_id.lower() in s["title"].lower()]
        if len(matches) != 1:
            raise AmigaError(f"{len(matches)} screens match {screen_id!r}; use the handle "
                             "from amiga_screens") from None
        return matches[0]


def _shot_behind(target: dict, found: list | None, region: tuple, text: bool) -> list:
    """A screen that is not in front, read straight from its bitmap; nothing moves."""
    x, y, width, height = region
    try:
        image = _with_screen(lambda: _session().screen.capture_screen(target))
    except AmigentdError as e:
        raise AmigaError(str(e)) from None
    w, h = width or target["width"] - x, height or target["height"] - y
    if region != (0, 0, 0, 0):
        image = image.crop((x, y, x + w, y + h))
    size = image.size
    image, scale = pictures.fit(image)
    note = (f"screen {target['title']!r} {target['width']}x{target['height']}, behind the front "
            f"one, read from its bitmap; region {size[0]}x{size[1]} at ({x},{y}); "
            f"screen_x = {x} + image_x * {scale:.3f}, screen_y = {y} + image_y * {scale:.3f}. "
            "Clicks and keys go to the front screen; amiga_screen_front brings this one forward")
    if shown := display_note(found or []):
        note += "\n" + shown
    out = [note, McpImage(data=pictures.png(image), format="png")]
    if text:
        out.append("no text: amiga_screen_text reads the front screen only")
    return out


def _slow_bus_note(region: tuple) -> str:
    """Once a session, for a whole screen on a Zorro II card (known from amiga_graphics)."""
    info, session = _session().graphics, _session()
    if not info or not info.slow_bus or any(region) or session.slow_bus_told:
        return ""
    session.slow_bus_told = True
    return ("the graphics card sits on Zorro II: each look reads its memory over the slow bus; "
            "a region, or changed=True after a click, keeps it quick")


def _screen_name(s: dict) -> str:
    kind = "graphics card" if s["rtg"] else "chipset"
    return f"{s['title']!r} ({s['width']}x{s['height']}, {kind}, {s['handle']:#010x})"


def display_note(found: list) -> str:
    """What the display shows besides the front Intuition screen; "" when nothing.

    found: SCREENS of an amigentd from 1.5.0 (older ones don't tell, and get "").
    The chipset output shows GfxBase->ActiView: Intuition's view with the
    frontmost chipset screen in it, or a program's own after LoadView. A graphics
    card shows its own frontmost screen; with a second monitor on the chipset
    output (or on the card's) both are visible at once."""
    if not found or not found[0].get("display"):
        return ""
    front = next((s for s in found if s["front"]), found[0])
    chip = next((s for s in found if s.get("chip_shown")), None)
    foreign = bool(found[0].get("foreign_view"))
    lines = []
    if front["rtg"]:
        if foreign:
            lines.append("the chipset output shows a display that is no Intuition screen: a program "
                         "loaded a view of its own (LoadView); this picture is the graphics card's "
                         "front screen, not that")
        elif chip:
            lines.append(f"the chipset output shows {_screen_name(chip)}, not in this picture; "
                         f"amiga_screenshot(screen='{chip['handle']:#010x}') shows it")
    else:
        if foreign:
            lines.append(f"the display shows something that is no Intuition screen: a program loaded "
                         f"a view of its own (LoadView); this picture is the front Intuition screen "
                         f"{_screen_name(front)}, which is not what the monitor shows")
        elif chip is not front:
            if chip:
                lines.append(f"the front screen is not in the chipset's view; the chipset output "
                             f"shows {_screen_name(chip)}")
            else:
                lines.append("the front screen is not in the chipset's view; the chipset output "
                             "shows no Intuition screen")
        card = next((s for s in found if s["rtg"]), None)
        if card:
            lines.append(f"the graphics card, if it has its own monitor, still shows "
                         f"{_screen_name(card)}; amiga_screenshot(screen='{card['handle']:#010x}') "
                         "shows it")
    return "\n".join(lines)


def _same_shot(region: tuple, seen: float) -> str:
    """A note instead of a picture when the model already holds this very picture.

    Same region, recent, no tile in it since, and the last answer was a real
    picture: a second ask in a row always gets one."""
    if not _session().last_shot or _session().last_shot[0] != region or _session().last_shot[2]:
        return ""
    when = _session().last_shot[1]
    if seen != when or time.monotonic() - when > SAME_SHOT:
        return ""  # someone looked elsewhere meanwhile, or it's been a while
    x, y, w, h = region
    if _session().screen.changed_since(when, x, y, w or None, h or None):
        return ""
    return (f"no change in this region since your screenshot {time.monotonic() - when:.0f} s "
            "ago, so no new picture; call again to get it anyway")


@mcp.tool()
def amiga_screenshot_color(x: int, y: int, width: int, height: int) -> list:
    """A region in full colour, for small text or exact colours.

    amiga_screenshot is cheaper and covers the whole screen in 256 colours;
    use this one for the part you need to read precisely.
    """
    if paused := _paused_note():
        return [paused]
    image = _with_screen(lambda: _session().screen.capture_rgb(x, y, width, height))
    _session().last_look = time.monotonic()  # after the fetch: what it brought is what was seen
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
    if paused := _paused_note():
        return paused
    # Tiles count whoever fetched them: an open viewer may have taken them first.
    report, changed = watch.wait_change(_session().screen, bind_callback(_with_screen), _session().lookout,
                                        (x, y, width or None, height or None),
                                        _session().last_look or time.monotonic(), timeout)
    if changed:
        _session().last_look = time.monotonic()
    return report


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
        left = _with_ctl(lambda: _session().ctl.pause_screen(seconds))
    except AttributeError:
        raise AmigaError("pausing the screen needs amigentd's command channel; run amigent-setup --update") from None
    _session().screen.note_pause(left)
    return f"screen paused for {left} s; amiga_pause_screen(0) ends it" if left else "screen pause ended"


def _paused_note() -> str:
    """While the Amiga pauses its screen, the note that stands in for a picture."""
    left = _with_screen(_session().screen.pause_left)
    if not left:
        return ""
    return (f"the screen is paused on the Amiga for another {left} s, so no picture is read; "
            "mouse, keys and commands still work. amiga_pause_screen(0) ends the pause")


@mcp.tool()
def amiga_screens() -> str:
    """List all Intuition screens, front to back, with their handles.

    rtg: on the graphics card; chipset-output: what the chipset puts out. A
    last line says when the display shows something other than the front screen."""
    found = _with_screen(_session().screen.screens)
    lines = [
        f"{s['handle']:#010x} {s['width']}x{s['height']}x{s['depth']}"
        f"{' front' if s['front'] else ''}{' workbench' if s['workbench'] else ''}"
        f"{' rtg' if s.get('rtg') else ''}{' ham' if s['ham'] else ''}"
        f"{' chipset-output' if s.get('chip_shown') else ''} {s['title']!r}" for s in found]
    if shown := display_note(found):
        lines.append(shown)
    return "\n".join(lines)


def _find_screen(screen_id: str) -> dict:
    """A screen by handle from amiga_screens or by part of its title."""
    return _pick_screen(_with_screen(_session().screen.screens), screen_id)


def _screen_handle(screen_id: str) -> int:
    return _find_screen(screen_id)["handle"]


@mcp.tool()
def amiga_screen_front(screen_id: str) -> str:
    """Bring a screen to the front, by handle from amiga_screens or by part of its title."""
    target = _find_screen(screen_id)
    if not _with_screen(lambda: _session().screen.screen_to_front(target["handle"])):
        raise AmigaError("that screen has just closed")
    _with_screen(_session().screen.check_size)
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
            _session().screen.move_relative(dx, dy)
            time.sleep(0.05)
            return _session().screen.pointer()
        start = _session().screen.pointer()
        _session().screen.walk_relative(dx, dy)
        return _session().screen.settle_pointer(start[0] + dx, start[1] + dy) if start else None

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
    tree = _with_screen(lambda: _session().screen.ui_tree(handle) if handle else _session().screen.ui_tree())
    if handle:
        got = next((i.get("handle") for i in tree if i["kind"] == "screen"), None)
        if got != handle:
            raise AmigaError("that screen has just closed, or amigentd is older than 1.3.0 "
                             "and only reads the front screen")
        pictures = pictures and _with_screen(_session().screen.screens)[0]["handle"] == handle
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
    picture = _with_screen(lambda: _session().screen.capture(*box))
    sheet = pictures.gadget_sheet(gadgets, picture, box[:2])
    return McpImage(data=pictures.png(sheet), format="png")


def _labels_from_text(gadgets: list[dict]) -> None:
    """Fill empty gadget labels with the screen text drawn inside their boxes."""
    if not any(not g["label"] for g in gadgets):
        return
    try:
        st = _screen_text()
    except AmigaError:
        return
    rows = [r for w in st.windows if not w.monitor for r in w.rows if not r.hidden]
    for g in gadgets:
        if g["label"] or g["type"] == "sys":
            continue
        inside = [r for r in rows if g["x"] <= r.centre[0] < g["x"] + g["w"] and g["y"] <= r.centre[1] < g["y"] + g["h"]]
        if inside:
            g["label"] = " ".join(r.text.strip() for r in inside)


SAFE_BUTTONS = ("suspend", "entfernen", "remove", "ignore", "ignorieren", "resume", "fortfahren",
                "continue", "weiter", "anhalten", "cancel", "abbrechen")
REBOOT_BUTTONS = ("reboot", "neustart", "neu starten", "reset")


@mcp.tool()
def amiga_software_failure(action: str = "show") -> str:
    """Deal with a "Software Failure" or "Task held" requester on the front screen.

    action show: its text, the guru code in words and the program at fault.
    action dismiss: click the button that leaves the Amiga running (Suspend,
    Entfernen, Ignore...), never Reboot. Registers of the crashed task are not
    in the requester; amiga_task(name, stack=8) reads where it stopped while
    it is suspended. Without one, a PowerPC crash window (WarpOS, PowerUP)
    is read instead: exception, task, registers, named; dismiss closes it.
    """
    from . import alerts

    st = _screen_text()
    wins = [w for w in st.windows if not w.monitor and any(k in (w.title + " " + " ".join(r.text for r in w.rows)).lower()
                                         for k in ("software failure", "software-fehler", "task held",
                                                   "softwarefehler", "task interferance", "task interference"))]
    if not wins:
        if action not in ("show", "dismiss"):
            raise AmigaError("action: show or dismiss")
        found = _ppc_crash(st, action)  # a WarpOS or PowerUP crash window instead
        return found or "no Software Failure requester on the front screen (amiga_screen_text shows what is there)"
    w = wins[0]
    text = "\n".join(r.text for r in w.rows)
    out = [f"requester {w.title!r} {w.id}:", text]
    m = re.search(r"\b([0-9A-Fa-f]{8})\b(?:[ .]+\(?(?:task[: ]*)?\$?([0-9A-Fa-f]{8}))?", text)
    if m:
        out.append("guru: " + alerts.describe(m.group(1), m.group(2)))
        hint = _fpu_hint(m.group(1))
        if hint:
            out.append("FPU: " + hint)
        if m.group(2):
            try:
                out.append(f"at {m.group(2)}: " + (_namer().name(int(m.group(2), 16)) or "no program amigentd knows"))
            except AmigaError:
                pass
    if action == "show":
        return "\n".join(out)
    if action != "dismiss":
        raise AmigaError("action: show or dismiss")
    tree = _with_screen(_session().screen.ui_tree)
    gadgets = [g for g in tree if g["kind"] == "gadget" and g["window"] == w.id and g["type"] != "sys"]
    _labels_from_text(gadgets)
    for g in gadgets:
        name = g["label"].lower().strip("_ ")
        if any(b in name for b in REBOOT_BUTTONS):
            continue
        if any(b in name for b in SAFE_BUTTONS):
            x, y = _centre(g)
            _with_screen(lambda: _session().screen.click(x, y, "left", False))
            return "\n".join(out + [f"clicked {g['label']!r}; the task stays suspended, the Amiga runs on"])
    for r in w.rows:  # buttons whose box amigentd can't see: click their text
        name = r.text.lower().strip()
        if any(b in name for b in SAFE_BUTTONS) and not any(b in name for b in REBOOT_BUTTONS):
            safe = next(b for b in SAFE_BUTTONS if b in name)
            px, py = screentext.point_of(r, safe)
            _with_screen(lambda: _session().screen.click(px, py, "left", False))
            return "\n".join(out + [f"clicked {safe!r} at ({px},{py}); the Amiga runs on"])
    raise AmigaError("found no Suspend or Entfernen button; not clicking, since the other one reboots:\n"
                     + "\n".join(out))


def _centre(item: dict) -> tuple[int, int]:
    return item["x"] + item["w"] // 2, item["y"] + item["h"] // 2


@mcp.tool()
def amiga_ui_click(label: str = "", window: str = "", number: int = -1, button: str = "left",
                   double: bool = False) -> str:
    """Click a gadget from amiga_ui_tree by its label, or by its #number within a window.

    window is part of the window's title, or its id from amiga_ui_tree.
    """
    tree = _with_screen(_session().screen.ui_tree)
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
        if not matches:  # requesters and buttons that draw their text themselves
            _labels_from_text(gadgets)
            exact = [g for g in gadgets if g["label"].lower().strip("_ ") == label.lower()]
            matches = exact or [g for g in gadgets if label.lower() in g["label"].lower()]
        if not matches:
            st = _screen_text()
            hit = screentext.find_row(st, label, window)
            if hit:
                win, row = hit
                px, py = screentext.point_of(row, label)
                _with_screen(lambda: _session().screen.click(px, py, button, double))
                return f"clicked the text {label!r} in {win.title!r} at ({px},{py}); no gadget carries that label"
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
    _with_screen(lambda: _session().screen.click(x, y, button, double))
    return f"clicked #{gadget['id']} {gadget['label']!r} at ({x},{y})"


@mcp.tool()
def amiga_menus() -> str:
    """The active window's menus and items, with shortcuts and state."""
    lines = []
    for m in _with_screen(_session().screen.menus):
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
    entries = _with_screen(_session().screen.menus)

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
        _session().screen.move(x, y)
        time.sleep(0.05)
        _session().screen.button("right", True)
        time.sleep(0.15)
        for entry in chain[1:]:
            _session().screen.move(*_centre(entry))
            time.sleep(0.15)
        _session().screen.button("right", False)

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
    _with_screen(lambda: _session().screen.click(x, y, button, double))
    return f"clicked {button} at ({x},{y})"


@mcp.tool()
def amiga_mouse_move(x: int, y: int) -> str:
    """Move the pointer to screen pixels without clicking, e.g. to show a tooltip."""
    _with_screen(lambda: _session().screen.move(x, y))
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
    _with_screen(lambda: _session().screen.button(button, down))
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
    before = _window_boxes()
    _with_screen(lambda: _session().screen.drag(x1, y1, x2, y2, button, relative))
    how = {True: " by mouse movement only", False: " in one jump"}.get(relative, "")
    return f"dragged {button} from ({x1},{y1}) to ({x2},{y2})" + how + _what_moved(before, x1, y1)


def _window_boxes() -> list[dict] | None:
    try:
        return [i for i in _with_screen(_session().screen.ui_tree) if i["kind"] == "window"]
    except (AmigaError, AmigentdError, OSError, KeyError, AttributeError):
        return None


def _what_moved(before: list[dict] | None, x: int, y: int) -> str:
    """Which windows moved or changed size, so a drag that caught the wrong one shows."""
    after = _window_boxes()
    if before is None or after is None:
        return ""
    was = {w["id"]: w for w in before}
    changes = []
    for w in after:
        b = was.get(w["id"])
        if b and (b["x"], b["y"], b["w"], b["h"]) != (w["x"], w["y"], w["w"], w["h"]):
            what = (f"moved from {b['x']},{b['y']} to {w['x']},{w['y']}" if (b["w"], b["h"]) == (w["w"], w["h"])
                    else f"changed from {b['x']},{b['y']} {b['w']}x{b['h']} to {w['x']},{w['y']} {w['w']}x{w['h']}")
            changes.append(f"window {w['title']!r} {what}")
    under = next((w for w in before if w["x"] <= x < w["x"] + w["w"] and w["y"] <= y < w["y"] + w["h"]), None)
    if not changes:
        return ("; no window moved" + (f" (the start point was on {under['title']!r})" if under else ""))
    text = "; " + "; ".join(changes)
    if under and not any(under["title"] and repr(under["title"]) in c for c in changes):
        text += f"; note: the start point was on {under['title']!r}, frontmost there"
    return text


SETTLE_FIRST = 0.3  # after Return, before the window counts: the Shell starts the command
SETTLE_QUIET = 0.6  # this long without a change and the window has settled


def _settle(box: tuple, wait: float) -> bool:
    """Wait until box has not changed for SETTLE_QUIET s, at most wait; True if it did.

    One changed tile is a blinking cursor, not output."""
    end = time.monotonic() + wait
    time.sleep(SETTLE_FIRST)
    quiet_since = time.monotonic()
    while time.monotonic() < end:
        if _session().screen.poll(*box) > 1:
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
        _with_screen(lambda: _session().screen.type_text(text))
        return f"typed {len(text)} characters"

    def run():
        box = next(((w["x"], w["y"], w["w"], w["h"]) for w in _session().screen.ui_tree()
                    if w["kind"] == "window" and w["active"]), None)
        late = 0
        for i, line in enumerate(lines):
            _session().screen.type_text(line)
            if box and line.endswith("\n") and i < len(lines) - 1 and not _settle(box, settle):
                late += 1
        return late

    late = _with_screen(run)
    note = f"; {late} line(s) went on before the window was still" if late else ""
    return f"typed {len(text)} characters in {len(lines)} lines{note}"


@mcp.tool()
def amiga_key(combo: str) -> str:
    """Press a key or combination: 'return', 'esc', 'lamiga+n', 'ctrl+c', 'f1', 'help'."""
    _with_screen(lambda: _session().screen.press(combo))
    return f"pressed {combo}"


@mcp.tool()
def amiga_hrtmon() -> str:
    """Load the HRTmon debugger resident on the Amiga, for the user at its keyboard.

    This only loads it; never enter it. HRTmon is entered by its hotkey or an
    NMI, and once entered it halts the whole system: the network and amigentd
    stop, and Amigent loses this Amiga until someone at the Amiga's own keyboard
    leaves the monitor. So never press its hotkey with amiga_key. HRTmon comes
    from Aminet (dev/debug/HRTmon), not with the OS; if it is not installed the
    reply says so.
    """
    from .hrtmon import LOADED, MISSING
    ctl = _session().ctl
    if _with_ctl(lambda: ctl.exec("Which >NIL: HRTmon")).rc != 0:
        return MISSING
    started = _with_ctl(lambda: ctl.exec("Run >NIL: HRTmon"))
    if started.rc != 0:
        raise AmigaError(f"HRTmon did not load: Run answered {started.rc}.")
    return LOADED


@mcp.tool()
def amiga_viewer() -> str:
    """Open a live window of the Amiga screen on the user's desktop for manual control.

    Use when the user wants to see the Amiga or drive it themselves: mouse,
    keyboard, Amiga key shortcuts, file upload. Your own screenshots and
    clicks keep working meanwhile and show up in the window.
    """
    if _session().viewer is None:
        _session().viewer = Viewer(_session().host, screen=_session().screen, lock=bind_callback(_screen_lock), ctl_port=CTL_PORT,
                        restart=bind_callback(amiga_restart), flush_volumes=FLUSH_VOLUMES, remember=True)
    url = _session().viewer.start(VIEWER_PORT)
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
        reboot(_session().ctl, FLUSH_VOLUMES)
        with _screen_lock():
            _session().screen.close()
        if not wait_gone(_session().host, SCREEN_PORT):
            raise AmigaError("the Amiga is still up: C:Reboot did not restart it")
        time.sleep(5)
        # amigentd greets even before a key
        if wait_for_port(_session().host, SCREEN_PORT, RESTART_WAIT if _power_available() else BOOT_WAIT):
            return "Amiga restarted"
        if not _power_available():
            raise AmigaError("no answer after restart; amiga_power('cycle') may be needed")
        attempts = power_cycle(_session().host, SCREEN_PORT)
    return f"Amiga restarted; it came back without network, so the power was cycled ({attempts}x)"


def _process_target():
    """Whether this session owns single-host environment settings."""
    if sessions.configured:
        return machines.identity(sessions.configured) == _session().host
    return len(machines.load()) <= 1


def _power_available():
    return power_available() and _process_target()


@mcp.tool()
def amiga_power(action: str) -> str:
    """Control mains power, if configured: 'on', 'off' or 'cycle'.

    Last resort for a hung Amiga: cutting power loses file changes from the
    last minutes. Prefer amiga_restart while the Amiga still answers.
    """
    action = action.strip().lower()
    if action not in ("on", "off", "cycle"):
        raise AmigaError("action must be 'on', 'off' or 'cycle'")
    if not _power_available():
        raise AmigaError("No power control bound to this machine; configure AMIGENT_HOST with AMIGENT_POWER_CMD.")
    if action == "cycle":
        with _exclusive():
            _session().ctl.close()
            with _screen_lock():
                _session().screen.close()
            attempts = power_cycle(_session().host, SCREEN_PORT)
        return f"Amiga is back after {attempts} power cycle(s)"
    set_power(action == "on")
    return f"power {action}"


@mcp.tool()
def amiga_pistorm(action: str = "status", text: str = "", file: str = "", lines: int = 100,
                  pi: str = "", confirm: bool = False) -> str:
    """The classic PiStorm emulator under Linux on the Amiga's Raspberry Pi, over SSH.

    Not for Emu68, which has no Linux. Untested on real hardware so far.
    The login uses the system's ssh with your own key (BatchMode, your
    known_hosts); Amigent stores no passwords. Read-only unless confirm=True.

    action: status (how the emulator runs, systemd unit or run script, which
    .cfg it reads, the Pi's temperature and under-voltage state), config (show
    the .cfg; file= another one), set_config (text= the whole new .cfg: without
    confirm the difference only; with confirm a copy stays on the Pi first),
    undo (puts the last copy back), restart (the way it was started), log
    (its last lines), set_pi (pi='[user@]address' of the Pi, '' removes it).
    """
    from . import pistorm
    action = action.strip().lower()
    host = _session().host
    if action == "set_pi":
        if not confirm:
            return f"set_pi would keep {pi or 'no Pi'} with this Amiga; confirm=True saves it"
        entry = pistorm.set_pi(host, pi)
        return f"{entry['name']}: Pi {entry.get('pi') or 'removed'}"
    board = pistorm.Pi(host)
    if action == "status":
        return board.status()
    if action == "config":
        path, content = board.read_config(file)
        return f"{path}:\n{content}"
    if action == "set_config":
        if not text.strip():
            raise AmigaError("set_config needs text= the whole new .cfg (read it first with action='config')")
        return board.write_config(text, file, confirm)
    if action == "undo":
        return board.undo(confirm)
    if action == "restart":
        return board.restart(confirm)
    if action == "log":
        return board.log(lines)
    raise AmigaError("action must be status, config, set_config, undo, restart, log or set_pi")


# ---- 1.3.0: text instead of pictures, steps in one call, waiting on the Amiga

def _amiga() -> str:
    return machines.identity(_session().host) or "Amiga"


def _screen_text() -> screentext.ScreenText:
    try:
        lines = _with_ctl(_session().ctl.screen_text)
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
    _with_screen(lambda: _session().screen.click(px, py, button, double))
    return f"clicked {text!r} in {win.title!r} at ({px},{py})"


WAIT_KINDS = {"window": 1, "window_gone": 2, "text": 3, "text_gone": 4}


def _window_ids() -> list[int]:
    try:
        return [int(w["id"], 16) for w in _with_screen(_session().screen.ui_tree) if w["kind"] == "window"]
    except (AmigaError, AmigentdError, OSError, ValueError, KeyError, AttributeError):
        return []


def _wait(kind: str, what: str, timeout: float, new: bool = False) -> str:
    code = WAIT_KINDS[kind] | (8 if new else 0)
    # Right after amiga_start, a window may already be open by the time the wait
    # begins: then "new" means not open before the start.
    since, ids = _session().windows_at_start
    known = ids if new and since and time.monotonic() - since < START_WINDOWS else None
    try:
        lines = _with_ctl(lambda: _session().ctl.wait_for(code, what, timeout, known))
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
    tiles = {(tx, ty): (tw, th) for t, tx, ty, tw, th in list(_session().screen.changes) if t > since}
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
    tiles = [(tx, ty, tw, th) for t, tx, ty, tw, th in list(_session().screen.changes) if t > since]
    if not tiles:
        return None
    x0 = min(t[0] for t in tiles)
    y0 = min(t[1] for t in tiles)
    x1 = max(t[0] + t[2] for t in tiles)
    y1 = max(t[1] + t[3] for t in tiles)
    return x0, y0, x1 - x0, y1 - y0


def changed_shot() -> list:
    """Only the parts of the screen that changed since the last look, with their place."""
    if paused := _paused_note():
        return [paused]
    since = _session().last_look
    _with_screen(lambda: _session().screen.capture())  # brings in every changed tile
    _session().last_look = time.monotonic()
    every = [(0, 0, _session().screen.width, _session().screen.height)]
    boxes = (_changed_boxes(since) if since else every) or []
    if not boxes:
        return ["no change on the screen since your last look"]
    total = len(_changed_boxes(since, 1000)) if since else 1
    out = []
    for x, y, w, h in boxes:
        image = _with_screen(lambda: _session().screen.capture(x, y, w, h))
        image, scale = pictures.fit(image)
        out.append(f"changed: {w}x{h} at ({x},{y}) of {_session().screen.width}x{_session().screen.height}; "
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
    policy = pack.policy_for(_session().host)
    if compress:
        try:
            policy.set_mode(compress)
        except ValueError as e:
            raise AmigaError(str(e)) from None
    if not policy.known:
        try:
            _with_ctl(_session().ctl.sysinfo)  # a channel learns the Amiga's rates on connect
        except AmigaError:
            pass
    return json.dumps(policy.describe(), indent=1)


# ---- developers


@mcp.tool()
def amiga_debug_log(everything: bool = False) -> str:
    """Debug output on the Amiga since the last call: kprintf, Enforcer and MuForce hits.

    With amigentd 1.4.0 the addresses of code in hits (PC: ..., by $...) get the
    program and hunk or the library function they belong to.

    amigentd catches what goes through exec's RawPutChar (as Sashimi does);
    the serial port still gets it as well. everything=True shows the whole
    ring (64 KB) instead of what is new. When the Pi's serial line of an
    Emu68 Amiga is recorded on this PC (amiga_serial_log), what came there is
    added, also while the Amiga does not answer.
    """
    serial = _serial_news(everything)
    try:
        total, data = _with_ctl(lambda: _session().ctl.debug_log(0 if everything else _session().debug_seen))
    except AttributeError:
        if serial:
            return serial
        raise AmigaError("debug output needs amigentd 1.3.0; run amigent-setup --update") from None
    except AmigaError as e:
        if serial:  # a frozen Amiga: what the Pi's serial line said is all there is
            return f"(the Amiga does not answer: {e})\n{serial}"
        raise
    _session().debug_seen = total
    text = data.decode("iso-8859-1").replace("\r\n", "\n").replace("\r", "\n")
    if text and memory._HIT.search(text):
        try:  # amigentd 1.4.0: name the code behind Enforcer, MuForce and CacheCop addresses
            text = memory.annotate(text, _namer())
        except AmigaError:
            pass
    if serial:
        return (text.rstrip("\n") + "\n" if text else "") + serial
    return text if text else "no new debug output"


SERIAL_SHOWN = 64 * 1024  # of the serial record at most this much per call, the newest


def _serial_news(everything: bool = False) -> str:
    """What the Pi's serial line gave since the last call, under its own heading; "" without a record."""
    try:
        if not serial_log.exists(_session().host):
            return ""
        size, text = serial_log.read_since(_session().host, 0 if everything else _session().serial_seen)
    except OSError:
        return ""
    _session().serial_seen = size
    if not text.strip():
        return ""
    if len(text) > SERIAL_SHOWN:
        text = "[...]\n" + text[-SERIAL_SHOWN:].split("\n", 1)[-1]
    return "--- the Pi's serial line (Emu68), recorded on this PC ---\n" + text.rstrip("\n")


@mcp.tool()
def amiga_follow(source: str = "debug", local_path: str = "", stop: bool = False,
                 every: float = 1.0) -> str:
    """Copy a growing log from the Amiga to this PC while it grows, so it survives a crash.

    source is "debug" (kprintf, Enforcer and MuForce output) or a file on the
    Amiga, such as a program's log. Every `every` seconds what is new goes to
    local_path (default: amigent's config folder, follow/<machine-id>/<name>.log), on a
    connection of its own. When the Amiga hangs, the copy holds everything up to
    that moment, also what its filesystem had not yet written to disk, with a
    marked line where the Amiga stopped answering. stop=True ends one (or all,
    with source ""); without arguments while followers run it lists them.
    """
    from . import follow, paths

    if stop:
        names = [source] if source else list(_session().followers)
        done = []
        for name in names:
            f = _session().followers.pop(name, None)
            if f:
                f.stop()
                done.append(f"{name}: {f.written} bytes in {f.target}")
        return "stopped " + "; ".join(done) if done else "nothing was being followed"
    if source in _session().followers:
        f = _session().followers[source]
        state = f" (Amiga not answering since {time.strftime('%H:%M:%S', time.localtime(f.lost_since))})" \
            if f.lost_since else ""
        return f"already following {source}: {f.written} bytes in {f.target}{state}"
    own = Commands(_session().host, CTL_PORT, SCREEN_PORT, timeout=15)
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
    target = Path(local_path).expanduser() if local_path else paths.FOLDER / "follow" / secret.safe(_session().host) / f"{name}.log"
    _session().followers[source] = follow.Follower(source, target, fetch, max(0.2, every), start).start()
    return (f"following {source} into {target}, from now on (what is already there: "
            f"amiga_get or amiga_debug_log)")


# ---- Emu68: the Pi's network (#20) and its serial line (#21)


def _is_emu68() -> bool | None:
    """devicetree.resource in LOADED; None when this amigentd can't tell (before 1.4.0)."""
    try:
        return any(lib.kind == "R" and lib.name.lower() == system.EMU68_RESOURCE
                   for lib in _loaded().libraries)
    except (AmigaError, AttributeError):
        return None


def _pinet_log(record: "emu68net.Record") -> str:
    try:
        return _with_ctl(lambda: _session().ctl.read_file(record.log)).decode("iso-8859-1").strip()
    except AmigaError:
        return ""


def _pinet_channel(address: str):
    """A command channel to this Amiga at address, proven by its key or pinned certificate."""
    from .channel import AmigaChannel
    chan = AmigaChannel(_session().host, SCREEN_PORT, timeout=15, address=address)
    chan.connect()
    return chan


def _pinet_keep(record: "emu68net.Record") -> str:
    """Through the new address: confirm, measure, keep the interface, move Amigent's entry."""
    said = _pinet_log(record)
    if said.startswith("restored"):
        record.state = "restored"
        emu68net.save(_amiga(), record)
        return (f"too late: {said}. Nothing is kept; amiga_pi_network('setup', ..., confirm=True) "
                "tries again (a longer probation helps a slow DHCP)")
    try:
        chan = _pinet_channel(record.new_address)
    except AmigaError as e:
        return (f"the Amiga does not answer at {record.new_address} yet ({e}); "
                f"amiga_pi_network('confirm') again before the probation of {record.probation} s ends, "
                "or let it run out and the old configuration stays alone")
    try:
        chan.write_file(emu68net.CONFIRM_FLAG, b"1\n")
        try:
            record.speed_after = emu68net.speed(chan)
        except AmigaError:
            pass
        chan.write_file(record.target, chan.read_file(record.staged))
        repair = emu68net.join(record.home, "repair.d")
        files.makedirs(chan, repair)
        chan.write_file(emu68net.join(repair, "pi-network.repair"),
                        emu68net.repair_module(record).encode("iso-8859-1"))
    finally:
        chan.close()
    old = machines.address(_session().host)
    machines.move(_session().host, record.new_address)
    _session().refresh_address()
    record.state = "kept"
    emu68net.save(_amiga(), record)
    faster = (f"; reading ran at {record.speed_before} KB/s before, {record.speed_after} KB/s now"
              if record.speed_before and record.speed_after else "")
    return (f"kept: {record.interface} answers at {record.new_address}; it is in "
            f"{record.target} now and comes up at every start. Amigent reaches this Amiga there "
            f"from now on (it was {old}){faster}. The old interface stays as it was; "
            "amiga_pi_network('undo') takes the new one back")


def _pinet_undo(record: "emu68net.Record") -> str:
    script = emu68net.undo_script(record)
    _with_ctl(lambda: _session().ctl.write_file("RAM:amigent-pinet-undo.script", script.encode("iso-8859-1")))
    result = _with_ctl(lambda: _session().ctl.exec("Execute RAM:amigent-pinet-undo.script", timeout=60))
    try:
        _with_ctl(lambda: _session().ctl.delete(
            emu68net.join(emu68net.join(record.home, "repair.d"), "pi-network.repair")))
    except AmigaError:
        pass
    moved = ""
    if record.old_address and machines.address(_session().host) == record.new_address:
        machines.move(_session().host, record.old_address)
        _session().refresh_address()
        moved = f"; Amigent reaches this Amiga at {record.old_address} again"
    record.state = "undone"
    emu68net.save(_amiga(), record)
    return (f"{record.interface} is down and {record.target} removed (rc={result.rc}); the other "
            f"interfaces were not touched{moved}")


def _pinet_setup(k: "emu68net.Kind", confirm: bool, probation: int) -> str:
    amiga, ctl = _amiga(), _session().ctl
    if not _is_emu68():
        raise AmigaError("this Amiga does not run Emu68 as far as amigentd tells (no devicetree.resource "
                         "in amiga_loaded): the Pi's network is only for PiStorm Amigas with Emu68")
    stack = _with_ctl(lambda: emu68net.stack(ctl))
    if not stack["can_set_up"]:
        raise AmigaError(f"the TCP/IP stack is {stack['name']}: {stack.get('note', '')}")
    if not _with_ctl(lambda: emu68net.exists(ctl, emu68net.join(emu68net.NETWORKS, k.device))):
        raise AmigaError(f"{k.device} is not in {emu68net.NETWORKS}: it comes from {k.source}. "
                         "Install it first (amiga_install_begin, then amiga_install_end)")
    target = emu68net.join(emu68net.NETINTERFACES, k.interface)
    if _with_ctl(lambda: emu68net.exists(ctl, target)):
        raise AmigaError(f"{target} exists already and is not Amigent's; nothing was changed. "
                         "Look at it with amiga_read_text")
    probation = max(30, min(int(probation), 900))
    record = emu68net.Record(kind=k.key, interface=k.interface, home=_home(),
                             old_address=machines.address(_session().host),
                             started=time.strftime("%Y-%m-%d %H:%M:%S"), probation=probation)
    if not confirm:
        wifi = (" For Wi-Fi the user first enters the network name and password on the Amiga, "
                "where wifipi.device expects them." if k.key == "wifi" else "")
        return (f"nothing changed. This would add {k.device} ({k.what}) as Roadshow interface "
                f"{k.interface} with DHCP, next to the existing interfaces, on probation for "
                f"{probation} s: the file goes to {record.staged} first, {record.script} takes the "
                f"interface down again unless the PC confirms through the new address, and only "
                f"then is it copied to {target}. Ask the user; to go on, "
                f"amiga_pi_network('setup', {k.key!r}, confirm=True).{wifi}")
    record.before = [e["name"] for e in _with_ctl(lambda: emu68net.names(ctl, emu68net.NETINTERFACES))]
    status = _with_ctl(lambda: ctl.exec("ShowNetStatus"))
    record.before_addresses = emu68net.addresses(status.output)
    try:
        record.speed_before = _with_ctl(lambda: emu68net.speed(ctl))
    except AmigaError:
        pass
    for path in (record.log, emu68net.CONFIRM_FLAG):
        try:
            _with_ctl(lambda: ctl.delete(path))
        except AmigaError:
            pass
    _with_ctl(lambda: files.makedirs(ctl, record.folder))
    _with_ctl(lambda: ctl.write_file(record.staged, emu68net.interface_file(k).encode("iso-8859-1")))
    _with_ctl(lambda: ctl.write_file(record.script, emu68net.probation_script(record).encode("iso-8859-1")))
    _with_ctl(lambda: ctl.write_file(emu68net.join(record.folder, "record.json"),
                                     record.to_json().encode("utf-8")))
    emu68net.save(amiga, record)
    _with_ctl(lambda: ctl.exec(f'Run >NIL: Execute "{record.script}"'))
    added = _with_ctl(lambda: ctl.exec(f'AddNetInterface "{record.staged}"', timeout=60))
    if added.rc:
        record.state = "restored"
        emu68net.save(amiga, record)
        return (f"AddNetInterface failed (rc={added.rc}): {added.output.strip()[-400:]}\n"
                f"nothing in {emu68net.NETINTERFACES} changed; the probation script ends by itself")
    deadline = time.monotonic() + max(5, min(45, probation - 20))
    while True:
        now = _with_ctl(lambda: ctl.exec("ShowNetStatus"))
        record.new_address = emu68net.new_address(record.before_addresses, emu68net.addresses(now.output),
                                                  record.old_address, machines.local_networks())
        if record.new_address or time.monotonic() >= deadline:
            break
        time.sleep(3)
    emu68net.save(amiga, record)
    if not record.new_address:
        return (f"{k.interface} is up but has no address yet (DHCP); amiga_pi_network('confirm') "
                f"within {probation} s keeps it, otherwise it goes down again by itself")
    return _pinet_keep(record)


@mcp.tool()
def amiga_pi_network(action: str = "report", interface: str = "ethernet", confirm: bool = False,
                     probation: int = emu68net.PROBATION) -> str:
    """The Raspberry Pi's Ethernet or Wi-Fi for an Amiga with Emu68, instead of a slow adapter.

    action "report" (the default, reads only): Emu68 or not, the drivers in
    DEVS:Networks (genet.device for the Pi 4's Ethernet, wifipi.device for
    Wi-Fi; a missing one is named with where it comes from), the TCP/IP stack,
    Roadshow's interfaces and addresses, and what the link runs at now.

    "setup" with interface "ethernet" or "wifi" adds the Pi's network as a
    second Roadshow interface next to the existing one, which stays as it is.
    Without confirm=True it only tells what it would do (ask the user first).
    The new interface comes up on probation: unless the PC reaches the Amiga
    through the new address within `probation` seconds, a script on the Amiga
    takes it down again, and a restart forgets it, so the way back needs no
    network. Once reached, it is kept (DEVS:NetInterfaces), the speed before
    and after is measured, and Amigent's entry moves to the new address.
    Wi-Fi names and passwords are entered by the user on the Amiga, never here.
    "confirm" retries reaching the new address; "undo" takes it all back.
    The Pi's boot partition is never touched.
    """
    action = action.strip().lower()
    record = emu68net.load(_amiga())
    if action == "report":
        emu68 = _is_emu68()
        facts = _with_ctl(lambda: emu68net.report(_session().ctl, bool(emu68),
                                                  pack.policy_for(_session().host).describe()))
        if emu68 is None:
            facts["emu68"] = "unknown: amigentd 1.4.0 tells (amigent-setup --update)"
        if record:
            facts["amigent_setup"] = {"interface": record.interface, "state": record.state,
                                      "new_address": record.new_address, "since": record.started}
            if record.state == "probation":
                facts["amigent_setup"]["probation_log"] = _pinet_log(record) or "running"
        return json.dumps(facts, indent=1)
    if action == "confirm":
        if not record or record.state != "probation":
            return "no interface on probation; amiga_pi_network('report') shows the state"
        if not record.new_address:
            status = _with_ctl(lambda: _session().ctl.exec("ShowNetStatus"))
            record.new_address = emu68net.new_address(record.before_addresses, emu68net.addresses(
                status.output), record.old_address, machines.local_networks())
            if not record.new_address:
                return f"{record.interface} has no address yet; amiga_pi_network('confirm') again"
        return _pinet_keep(record)
    if action == "undo":
        if not record or record.state == "undone":
            return "nothing to undo: Amigent set up no Pi network on this Amiga"
        return _pinet_undo(record)
    if action != "setup":
        raise AmigaError("action is report, setup, confirm or undo")
    k = emu68net.kind(interface)
    if record and record.state in ("probation", "kept"):
        return (f"{record.interface} is set up already ({record.state}); amiga_pi_network('undo') "
                "first to set it up again")
    return _pinet_setup(k, confirm, probation)


@mcp.tool()
def amiga_serial_log(action: str = "status", port: str = "", baud: int = serial_log.BAUD,
                     lines: int = 40, low: str = "", high: str = "", verbose: int = 1,
                     disassemble: bool = False, confirm: bool = False) -> str:
    """The Pi's serial debug output (Emu68), recorded on this PC through a USB serial adapter.

    "start" records port (COM5, /dev/ttyUSB0; default the only USB adapter) at
    baud (921600, Emu68's speed) with time stamps until "stop", into the
    config folder (serial/<machine-id>.log); `amigent-serial` does the same on
    its own. Only reads. amiga_debug_log then includes what came, and
    amiga_status gives the last lines when the Amiga stops answering.
    "status" tells whether it records, "show" the last `lines` lines.

    "narrow" limits Emu68's JIT debug output to the 68k addresses low..high
    (hex, e.g. $00F80000) with verbosity 0-3 and disassemble, through Emu68's
    control registers at run time; "wide" switches that off again (verbosity 0,
    every address). Both run a small program on the Amiga and need
    confirm=True; only on Emu68.
    """
    action = action.strip().lower()
    host = _session().host
    if action == "start":
        try:
            rec = serial_log.start(host, port, baud)
        except (RuntimeError, OSError) as e:
            raise AmigaError(str(e)) from None
        return f"recording {rec.name} at {rec.baud} baud into {rec.target}; amiga_serial_log('stop') ends it"
    if action == "stop":
        if serial_log.stop(host):
            return "stopped"
        return "this server was not recording; amigent-serial stops with Ctrl+C"
    if action == "status":
        return json.dumps(serial_log.status(host), indent=1)
    if action == "show":
        found = serial_log.tail(host, max(1, min(lines, 2000)))
        return "\n".join(found) if found else "nothing recorded from the serial line yet"
    if action not in ("narrow", "wide"):
        raise AmigaError("action is start, stop, status, show, narrow or wide")
    try:
        if action == "wide":
            a, b, verbose, disassemble = 0, 0xFFFFFFFF, 0, False
        else:
            a, b = (int(v.strip().lstrip("$").removeprefix("0x"), 16) for v in (low, high))
        program = serial_log.range_program(a, b, verbose, disassemble)
    except ValueError as e:
        raise AmigaError(f"low and high are hex addresses such as $00F80000 ({e})") from None
    if not _is_emu68():
        raise AmigaError("these control registers exist only on Emu68 (no devicetree.resource in "
                         "amiga_loaded); on another CPU the program would crash")
    if action == "narrow":
        what = (f"Emu68's debug output to ${a:08X}..${b:08X}, verbosity {verbose}"
                + (", disassembled" if disassemble else ""))
    else:
        what = "Emu68's debug output back to verbosity 0 for every address"
    if not confirm:
        return f"nothing changed. Would set {what}. Ask the user; then the same call with confirm=True"
    path = "RAM:amigent-emu68dbg"
    _with_ctl(lambda: _session().ctl.write_file(path, program))
    result = _with_ctl(lambda: _session().ctl.exec(path, timeout=20))
    if result.rc:
        raise AmigaError(f"the program ended with rc={result.rc}: {result.output.strip()[-300:]}")
    return f"set {what}; only code translated from now on follows it (the JIT cache was cleared)"


RUN_DIR = "RAM:amigent-run"
CRASH_TITLES = ("software failure", "softwarefehler", "program failed", "programm fehlgeschlagen",
                "guru", "systemanforderung", "system request", "powerpc exception", "ppc exception",
                "ppc crash")


FPU_CHECK_MAX = 1 << 20  # bytes read from the Amiga for the FPU check; plipbox does ~17 KB/s


def _fpu_check(program: str, local: Path, check_fpu: bool) -> tuple[str, bool]:
    """(note, stop) before running: does the program use FPU instructions this Amiga cannot run?

    Only asked when this Amiga has no FPU, or a 68040/060 FPU without its
    library; the program is read from the Amiga only then and up to 1 MB.
    stop is True when FPU code was found and the Amiga has no FPU at all.
    """
    from . import hunks
    if not check_fpu:
        return "", False
    fpu = _fpu()
    if fpu is None or not (fpu.present is False or fpu.support_missing):
        return "", False
    if local.is_file():
        data = local.read_bytes()
    else:
        path = program.strip().strip('"')
        if ":" not in path and "/" not in path:
            return "", False  # a command found through the path; not looked for
        try:
            size = _with_ctl(lambda: _session().ctl.stat(path)).get("size", 0)
            if not 0 < int(size) <= FPU_CHECK_MAX:
                return "", False
            data = _with_ctl(lambda: _session().ctl.read_file(path))
        except (AmigaError, TypeError, ValueError):
            return "", False
    try:
        use = hunks.fpu_use(data)
    except hunks.NotAProgram:
        return "", False
    if not use.uses_fpu:
        return "", False
    if fpu.present is False:
        return (f"not run: {Path(program).name} {use.describe()}, and this Amiga has none "
                f"({fpu.line()}), so it would most likely crash with a line-F guru. "
                "amiga_run(..., check_fpu=False) runs it anyway"), True
    if use.emulated:
        return (f"FPU check: {Path(program).name} {use.describe()}; {fpu.line()}, so those "
                "may crash"), False
    return "", False


@mcp.tool()
def amiga_run(program: str, args: str = "", timeout: float = 60, cd: str = "",
              check_fpu: bool = True) -> str:
    """Develop and test: copy a program over if it is local, run it, and report everything.

    program is a path on this PC (uploaded to RAM:amigent-run first, only when
    it changed) or on the Amiga. Reports return code and output, the debug
    output it produced (kprintf, Enforcer hits), requesters that came up
    meanwhile (such as Software Failure; a WarpOS crash window is read and
    explained like a guru) and, if the Amiga dropped off, what
    amiga_status finds after it came back: a restart or a guru code.

    check_fpu: on an Amiga without an FPU, a program whose code holds FPU
    instructions is not started (it would crash with a line-F guru); on a
    68040/060 without its support library it is started with a warning. The
    check is a heuristic over the code hunks; check_fpu=False skips it.
    """
    local = Path(program).expanduser()
    fpu_note, stop = _fpu_check(program, local, check_fpu)
    if stop:
        return fpu_note
    if local.is_file():
        _with_ctl(lambda: files.makedirs(_session().ctl, RUN_DIR))
        target = _join(RUN_DIR, local.name)
        _with_ctl(lambda: files.put(_session().ctl, local, target))
        _with_ctl(lambda: _session().ctl.exec(f'Protect "{target}" +rwed'))
        command = f'"{target}" {args}'.strip()
    else:
        command = f"{program} {args}".strip()
    try:
        seen_before, _ = _with_ctl(lambda: _session().ctl.debug_log(0xFFFFFFFF))
    except (AttributeError, AmigaError):
        seen_before = None
    windows_before = _window_titles()
    report = [fpu_note] if fpu_note else []
    ppc_note = _ppc_program_note(local)
    if ppc_note:
        report.append(ppc_note)
    try:
        result = _with_ctl(lambda: _session().ctl.exec(command, cd or None, timeout))
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
            total, data = _with_ctl(lambda: _session().ctl.debug_log(seen_before))
            _session().debug_seen = total
            if data:
                report.append("debug output:\n" + data.decode("iso-8859-1").rstrip())
        except AmigaError:
            pass
    new = [t for t in _window_titles() if t not in windows_before]
    if new:
        report.append("new windows: " + ", ".join(repr(t) for t in new))
        if any(any(c in t.lower() for c in CRASH_TITLES) for t in new):
            crash = ""
            if any(w in t.lower() for t in new for w in ("powerpc", "ppc")):
                try:
                    crash = _ppc_crash(_screen_text())
                except AmigaError:
                    crash = ""
            if crash:
                report.append(crash + "\namiga_software_failure('dismiss') closes the window")
            else:
                report.append("one looks like a crash requester: amiga_screen_text or a screenshot "
                              "shows it; answer it before running again")
    return "\n".join(report)


def _ppc_program_note(local: Path) -> str:
    """For a WarpOS program from this PC: whether this Amiga has WarpOS to run it."""
    from . import hunks
    try:
        if not local.is_file() or local.stat().st_size > (8 << 20) or not hunks.ppc_hunks(local.read_bytes()):
            return ""
    except (OSError, hunks.NotAProgram):
        return ""
    try:
        on_disk = _system_facts().get("ppc", "")
    except AmigaError:
        return ""
    if "WarpOS" in on_disk:
        return ""
    return ("note: this is a WarpOS program (PowerPC code), and this Amiga has no powerpc.library "
            "in LIBS:; it will most likely not start")


def _window_titles() -> list[str]:
    try:
        return [w["title"] for w in _with_screen(_session().screen.ui_tree) if w["kind"] == "window"]
    except (AmigaError, OSError):
        return []


# ---- clipboard

@mcp.tool()
def amiga_clipboard(text: str | None = None) -> str:
    """Read the Amiga's clipboard, or set it to text (then RAmiga+V pastes it there)."""
    from .viewer import CLIPBOARD, MAX_CLIP, ftxt, ftxt_text
    if text is not None:
        _with_ctl(lambda: _session().ctl.write_file(CLIPBOARD, ftxt(text)))
        return f"clipboard set ({len(text)} characters)"
    try:
        size = int(_with_ctl(lambda: _session().ctl.stat(CLIPBOARD)).get("size", 0) or 0)
    except AmigaError as e:
        if e.code is None:
            raise
        return "the clipboard is empty"
    if size > MAX_CLIP:
        return "the clipboard holds more than text (a picture, say)"
    return ftxt_text(_with_ctl(lambda: _session().ctl.read_file(CLIPBOARD))) or "the clipboard holds no text"


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


def _volume_free(path: str) -> tuple[str, int, int] | None:
    """(volume, free bytes, block size) of the drive path is on; None when that
    can't be told (an assign is followed to its folder, a device name is not)."""
    try:
        vols = _with_ctl(lambda: _session().ctl.volumes())
    except (AmigaError, AttributeError):
        return None
    by_name = {str(v["name"]).rstrip(":").lower(): v for v in vols}
    name = path.split(":", 1)[0]
    for _ in range(4):
        v = by_name.get(name.lower())
        if v:
            return str(v["name"]), int(v["free"]), int(v.get("blocksize") or 512)
        try:
            found = _with_ctl(lambda: _session().ctl.exec(f"Assign {name}: EXISTS", timeout=15))
        except AmigaError:
            return None
        dirs = install.parse_assign(found.output) if found.rc == 0 else []
        if not dirs or ":" not in dirs[0] or dirs[0].split(":", 1)[0].lower() == name.lower():
            return None
        name = dirs[0].split(":", 1)[0]
    return None


def _marker_ok(record: install.Record) -> bool:
    """The backup's marker is there and belongs to this record (not an older one of the name)."""
    try:
        data = _with_ctl(lambda: _session().ctl.read_file(record.marker))
    except AmigaError:
        return False
    return data.decode("iso-8859-1").strip() == record.started


def _backup_state(record: install.Record) -> str:
    """complete, running or failed. A running backup is looked at on the Amiga:
    the marker makes it complete, a job that ended (or is gone, after a
    restart) without it failed."""
    if record.backup in ("", "complete"):
        return "complete"
    if record.backup == "failed":
        return "failed"
    if _marker_ok(record):
        state = "complete"
    else:
        try:
            _with_ctl(lambda: _session().ctl.stat(_job_file(record.job, "cmd")))
            gone = False
        except AmigaError:
            gone = True
        if not gone and _job_ended(record.job) is None:
            return "running"
        state = "complete" if _marker_ok(record) else "failed"
    record.backup = state
    install.save(_amiga(), record)
    return state


def _install_ready(record: install.Record, path) -> str:
    copied = install.estimate(record, record.copied or [])
    lines = [f"ready: {record.estimate.get('entries', 0)} entries in {len(record.paths)} folders "
             f"recorded, {copied['files']} files ({install.size_text(copied['bytes'])}) copied to "
             f"{record.store}"]
    if not record.full and record.nocrc:
        lines.append("the system folders are recorded, not copied: undo removes what the "
                     "installation adds there, but can restore only the system files that "
                     "were copied (touch=..., full=True, and what amiga_aminet_install or "
                     "amiga_put overwrite from now on); amiga_install_end names the others")
    lines.append(f"install now, then amiga_install_end({record.name!r}). Record: {path}")
    whole = _install_checkpoint(record.name)
    return "\n".join(lines + ([whole] if whole else []))


@mcp.tool()
def amiga_install_begin(name: str, folders: str = "", system: bool = True, touch: str = "",
                        full: bool = False, confirm: bool = False, estimate_only: bool = False,
                        wait: float = 120) -> str:
    """Before installing something: take stock so it can be undone, and copy aside what undo needs.

    folders (comma separated, e.g. the program's target folder) are recorded
    and copied aside whole. With system=True (the default) C:, LIBS:, DEVS:,
    S:, L:, ENVARC:, SYS:WBStartup and SYS:Classes are recorded as well (name,
    size, date), but not copied: of them only S:Startup-Sequence,
    S:User-Startup and the files in touch (comma separated, e.g.
    'LIBS:foo.library') are copied, plus what amiga_aminet_install and
    amiga_put are about to overwrite while the installation is open.
    full=True copies the system folders whole (slow on a full system).
    system=False watches only folders, for a game that stays in its drawer.

    A record can tell that a file changed but not bring its old content back:
    amiga_install_end and amiga_install_undo name each system file that changed
    or went without a copy. When the installer is known to write system files,
    name them in touch.

    First comes an estimate (files, bytes, time) and a check of the free
    space; above 2000 files, 32 MB or 5 minutes nothing is copied until the
    call is repeated with confirm=True (ask the user first). estimate_only=True
    only tells. The copying runs as a background job: after wait seconds the
    answer gives its number for amiga_job_output (progress) and
    amiga_break(job=n); a backup stopped or failed is never used. Calling this
    again says whether it is complete. Then install as usual and call
    amiga_install_end(name).
    """
    name = install.name_ok(name)
    amiga = _amiga()
    try:
        old = install.load(amiga, name)
    except AmigaError:
        old = None
    note = ""
    if old and not old.done and old.backup == "running":
        state = _backup_state(old)
        if state == "running":
            return (f"the backup for {name!r} still runs as job {old.job}: amiga_job_output({old.job}) "
                    f"shows how far it got, amiga_break(job={old.job}) stops it. Do not install yet")
        if state == "complete":
            return _install_ready(old, install.folder_for(amiga) / f"{name}.json")
        note = "the earlier backup did not complete (stopped or failed); starting over\n"
    extra = [f.strip() for f in folders.split(",") if f.strip()]
    touched = [t.strip() for t in touch.split(",") if t.strip()]
    base = list(install.SYSTEM) if system else []
    if not base and not extra:
        raise AmigaError("with system=False, name the folders to watch")
    if full and not system:
        raise AmigaError("full=True copies the system folders whole; it needs system=True")
    paths, aliases = _with_ctl(lambda: install.expand_map(_session().ctl, base + [f for f in extra if f not in base]))
    record = install.Record(name, _home(), paths, started=time.strftime("%Y-%m-%d %H:%M:%S"),
                            aliases=aliases, full=full)
    began = time.monotonic()
    for path in record.paths:
        snap = _with_ctl(lambda: install.snapshot(_session().ctl, path, crc=False))
        if snap is None:
            record.missing.append(path)
        record.before[path] = snap or {}
    listed = time.monotonic() - began
    copies = install.plan(record, extra, touched)
    est = install.estimate(record, copies, install.factor(amiga), listed=listed)
    lines = [install.describe(est)]
    space = _volume_free(record.home)
    if space:
        volume, free, block = space
        need = install.needed_space(est, block)
        if need > free:
            raise AmigaError(f"not enough room on {volume} for the backup in {record.store}: about "
                             f"{install.size_text(need)} needed, {install.size_text(free)} free. "
                             "Free some, or copy less (fewer folders, full=False); nothing was copied")
        lines.append(f"{volume} has {install.size_text(free)} free, the copies need about "
                     f"{install.size_text(need)}")
    else:
        lines.append(f"the free space for {record.store} could not be checked")
    if estimate_only:
        return "\n".join(["estimate only, nothing recorded or copied:"] + lines)
    over = install.over_limit(est)
    if over and not confirm:
        return "\n".join([f"the backup would be large: {', '.join(over)}. Nothing was recorded or "
                          "copied. Ask the user; to go on, amiga_install_begin(..., confirm=True). "
                          "Less to copy: full=False, fewer folders, or only the system files the "
                          "installation writes in touch"] + lines)
    wanted = {e.lower() for e in extra} | {d.lower() for n, ds in aliases.items()
                                           if n.lower() in {e.lower() for e in extra} for d in ds}
    crc_paths = [p for p in copies if p in record.paths and p.lower() in wanted]
    for path in crc_paths:  # what is copied whole and was named: compared by content at the end
        record.before[path] = _with_ctl(lambda: install.snapshot(_session().ctl, path)) or {}
    record.nocrc = [p for p in record.paths if p not in crc_paths]
    record.copied, record.estimate, record.backup = copies, est, "running"
    try:  # an old marker of this name must not make a new backup look complete
        _with_ctl(lambda: _session().ctl.delete(record.marker))
    except AmigaError:
        pass
    record.job = _start_job(install.begin_script(record), title=f"Amigent backup for installation {name}")
    saved = install.save(amiga, record)
    started = time.monotonic()
    said = _wait_job(record.job, wait)
    if said is None:
        return note + "\n".join(lines + [
            f"the backup runs as job {record.job}: amiga_job_output({record.job}) shows how far it "
            f"got, amiga_break(job={record.job}) stops it (a stopped backup is not used). "
            f"amiga_install_begin({name!r}) again says when it is complete; install only then"])
    if _marker_ok(record):
        install.learn(amiga, install.estimate(record, copies)["seconds"], time.monotonic() - started)
        record.backup = "complete"
        install.save(amiga, record)
        return note + "\n".join(lines) + "\n" + _install_ready(record, saved)
    record.backup = "failed"
    install.save(amiga, record)
    raise AmigaError(f"the backup did not complete and will not be used: {said[-600:]}\n"
                     f"amiga_install_begin({name!r}) again starts over; nothing is installed yet")


def _install_checkpoint(name: str) -> str:
    """With an emulator debugger set, the whole machine as a state file as well."""
    if not _emulator_endpoint():
        return ""
    target = _checkpoint_folder() / f"install-{name}.uss"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _emulator().checkpoint("save", _host_path(str(target)))
    except (AmigaError, gdb.GdbError, OSError) as e:
        return f"(no emulator checkpoint: {e})"
    return (f"the whole emulated Amiga is saved as {target}; amiga_emulator('restore', "
            f"file={str(target)!r}) puts everything back as it was, memory included")


def _open_installs() -> list[install.Record]:
    """Installations begun (1.5.0 records) and not yet ended."""
    return [r for r in install.listing(_amiga())
            if not r.done and r.copied is not None and r.backup in ("running", "complete")]


def _installs_open() -> bool:
    try:
        return bool(_open_installs())
    except AmigaError:
        return False


def _install_guard(fulls: list[str]) -> str:
    """Before something overwrites files: the recorded files of an open
    installation that have no copy yet are copied aside first."""
    if not fulls:
        return ""
    said = []
    for record in _open_installs():
        todo = install.to_guard(record, fulls)
        if not todo:
            continue
        script = install.guard_script(record, todo)
        _with_ctl(lambda: _session().ctl.write_file("RAM:amigent-guard.script", script.encode("iso-8859-1")))
        result = _with_ctl(lambda: _session().ctl.exec("Execute RAM:amigent-guard.script", timeout=600))
        if result.rc:
            raise AmigaError(f"could not copy {', '.join(todo[:6])} aside for the installation "
                             f"{record.name!r} before overwriting (rc={result.rc}): "
                             f"{result.output[-300:]}; nothing was written")
        record.copied += todo
        install.save(_amiga(), record)
        said.append(f"copied {len(todo)} file(s) aside for the installation {record.name!r} "
                    "before overwriting them")
    return "; ".join(said)


@mcp.tool()
def amiga_install_end(name: str) -> str:
    """After installing: what came, changed and went; keeps what undo needs, writes a repair module.

    Names the system files that changed or went without a copy from before:
    undo can't restore those."""
    record = install.load(_amiga(), name)
    if record.done:
        return f"{name!r} was finished already; amiga_install_undo({name!r}) takes it back"
    state = _backup_state(record)
    if state == "running":
        raise AmigaError(f"the backup for {name!r} still runs (job {record.job}); "
                         f"amiga_install_begin({name!r}) says when it is complete")
    if state == "failed":
        raise AmigaError(f"the backup for {name!r} did not complete, so there is nothing to undo "
                         f"from. If nothing is installed yet, amiga_install_begin({name!r}) again")
    kinds: dict[str, dict] = {}  # full path -> entry as it is after the installation
    for path in record.paths:
        after = _with_ctl(lambda: install.snapshot(_session().ctl, path, crc=path not in record.nocrc))
        added, changed, removed = install.compare(record.before.get(path), after)
        for rel in added + changed:
            kinds[install.join(path, rel)] = (after or {})[rel]
        record.added += [install.join(path, r) for r in added]
        record.changed += [install.join(path, r) for r in changed]
        record.removed += [install.join(path, r) for r in removed]
    record.unrestorable = [f for f in record.changed + record.removed if not record.has_copy(f)]
    keeps = record.added or record.changed
    for script, name_ in ((install.prune_script(record), "prune"),
                          (install.apps_script(record, kinds) if keeps else "", "apps")):
        if not script:
            continue
        _with_ctl(lambda: _session().ctl.write_file(f"RAM:amigent-install-{name_}.script", script.encode("iso-8859-1")))
        _with_ctl(lambda: _session().ctl.exec(f"Execute RAM:amigent-install-{name_}.script", timeout=3600))
    notes_ = []
    if keeps:
        module = install.repair_module(record, kinds)
        target = install.join(install.join(record.home, "repair.d"), f"{record.name}.repair")
        _with_ctl(lambda: files.makedirs(_session().ctl, install.join(record.home, "repair.d")))
        _with_ctl(lambda: _session().ctl.write_file(target, module.encode("iso-8859-1")))
        notes_.append(f"repair module {target}")
    record.done = True
    path = install.save(_amiga(), record)
    _with_ctl(lambda: _session().ctl.write_file(install.join(record.store, "record.json"),
                                     record.to_json().encode("utf-8", "replace")))

    def some(items):
        return ", ".join(items[:12]) + (f" and {len(items) - 12} more" if len(items) > 12 else "")
    lines = [f"{name!r}: {len(record.added)} added, {len(record.changed)} changed, "
             f"{len(record.removed)} removed"]
    for label, items in (("added", record.added), ("changed", record.changed), ("removed", record.removed)):
        if items:
            lines.append(f"{label}: {some(items)}")
    if record.unrestorable:
        lines.append(f"no copy from before, so undo can't restore them: {some(record.unrestorable)}")
    lines += notes_ + [f"amiga_install_undo({name!r}) takes it back. Record: {path}"]
    return "\n".join(lines)


@mcp.tool()
def amiga_install_undo(name: str) -> str:
    """Take an installation back: remove what it added, restore what it changed or removed.

    Only what the backup holds can be restored; system files that changed or
    went without a copy are named, the rest is as before."""
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
                        snap_type = _with_ctl(lambda: _session().ctl.stat(full)).get("type")
                    except AmigaError:
                        pass
                    if snap_type == "dir":
                        dirs.append(full)
    lost = [f for f in record.changed + record.removed if not record.has_copy(f)]
    script = install.undo_script(record, dirs)
    _with_ctl(lambda: _session().ctl.write_file("RAM:amigent-undo.script", script.encode("iso-8859-1")))
    result = _with_ctl(lambda: _session().ctl.exec("Execute RAM:amigent-undo.script", timeout=3600))
    left = []
    for path in record.paths:
        after = _with_ctl(lambda: install.snapshot(_session().ctl, path, crc=path not in record.nocrc))
        added, changed, removed = install.compare(record.before.get(path), after)
        left += [install.join(path, r) for r in added + changed + removed]
    lost_now = [f for f in lost if f in left]
    left = [f for f in left if f not in lost]
    module = install.join(install.join(record.home, "repair.d"), f"{record.name}.repair")
    try:
        _with_ctl(lambda: _session().ctl.delete(module))
    except AmigaError:
        pass
    record.undone = not left
    record.unrestorable = lost_now
    install.save(_amiga(), record)
    unsaid = ""
    if lost_now:
        unsaid = (f"\nnot restored, there was no copy from before the installation: "
                  f"{', '.join(lost_now[:12])}" + (f" and {len(lost_now) - 12} more" if len(lost_now) > 12 else "")
                  + "; they stay as the installation left them (a repair, the program's own "
                  "uninstaller or a backup of the system brings them back)")
    if left:
        return (f"{name!r} taken back except {len(left)} entries (rc={result.rc}): "
                + ", ".join(left[:12]) + unsaid)
    if lost_now:
        return f"{name!r} taken back as far as the backup goes: what came is gone" + unsaid
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
    _with_ctl(lambda: files.makedirs(_session().ctl, target))
    has_tool = False
    if tool:
        found = _with_ctl(lambda: _session().ctl.exec(f"Which {tool}", timeout=15))
        has_tool = found.rc == 0
    guarded = ""
    if has_tool:
        if _installs_open():  # copy aside what the archive will overwrite, before it does
            inside = _archive_members(name, data)
            if inside is None:
                guarded = (f"; note: Amigent can't look into {name}, so files it overwrote in "
                           "recorded system folders have no copy for undo")
            else:
                guarded = _install_guard([_join(target, "/".join(p)) for p in map(_member_parts, inside) if p])
        archive = _join("T:", name)
        _with_ctl(lambda: _session().ctl.write_file(archive, data))
        result = _with_ctl(lambda: _session().ctl.exec(aminet.unpack_line(tool, archive, target), timeout=900))
        _with_ctl(lambda: _session().ctl.delete(archive))
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
            if _installs_open():
                guarded = _install_guard([_join(target, "/".join(p))
                                          for p in map(_member_parts, members) if p])
            _with_ctl(lambda: files.put(_session().ctl, Path(tmp), target))
        how = f"unpacked on the PC ({len(members)} files) and sent"
    top = _with_ctl(lambda: _session().ctl.list_dir(target))
    return (f"{name} ({len(data) // 1024} KB) {how} into {target}. {target} now holds: "
            + ", ".join(e["name"] + ("/" if e["type"] == "DIR" else "") for e in top[:30])
            + (guarded if guarded.startswith(";") else f"; {guarded}" if guarded else ""))


def _archive_members(name: str, data: bytes) -> list[str] | None:
    """The file names in an .lha or .zip archive, read on the PC; None for others."""
    import io
    import struct
    import zipfile
    from . import lzh
    lower = name.lower()
    try:
        if lower.endswith((".lha", ".lzh")):
            return [n for n, method, _, _ in lzh.entries(data) if method != b"-lhd-" and not n.endswith("/")]
        if lower.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                return [i.filename for i in z.infolist() if not i.is_dir()]
    except (ValueError, IndexError, KeyError, zipfile.BadZipFile, struct.error):
        return None
    return None


@mcp.tool()
def amiga_update(apply: bool = False, skip: bool = False) -> str:
    """Is a newer Amigent out? With apply=True, update this PC and then every saved Amiga.

    Without apply it only looks (on PyPI) and tells what is new and what an update
    would do. Use apply=True only after the user said yes. It runs pipx upgrade
    amigent, then amigent-setup --update for each Amiga in amigas.json; the user
    restarts the AI app afterwards to get the new tools. skip=True stops offering
    this version.
    """
    state = update.refresh(force=True)
    found = update.offer(state)
    if skip:
        if found:
            update.skip(found["version"])
            return f"Amigent {found['version']} will not be offered again."
        return "nothing to skip"
    if not update.enabled():
        return "The update check is off (python -m amigent.update on)."
    if not found:
        latest = state.get("latest") or "unknown (PyPI did not answer)"
        return f"Amigent {__version__} is the newest; on PyPI: {latest}."
    amigas = [a["name"] for a in machines.load()]
    kind = update.how_installed()
    steps = " ".join(update.command(kind)) or "git pull in the checkout"
    if not apply:
        return (f"Amigent {found['version']} is out; this PC has {found['current']}.\n"
                f"{found['notes'] or 'What is new: ' + found['page']}\n\n"
                f"Updating runs: {steps}, then amigent-setup --update for "
                f"{', '.join(amigas) or 'no saved Amiga'}. Ask the user first.")
    if kind == "source":
        return f"This Amigent runs from a source checkout: {steps}, then amigent-setup --update."
    said: list[str] = []
    _close_for_update()
    ok = update.apply(amigas, say=said.append)
    if ok:
        said.append("Done. Restart the AI app now, and the remote desktop if it is open, "
                    "so they run the new version.")
    return "\n".join(said)


# ---- Emu68's boot partition on the SD card (#18, #19)


def _emu68_card(volume: str = ""):
    """The boot partition as a card, after making sure this Amiga runs Emu68."""
    from . import emu68card
    try:
        loaded = _loaded()
        if not any(lib.kind == "R" and lib.name.lower() == system.EMU68_RESOURCE for lib in loaded.libraries):
            raise AmigaError("this Amiga does not run Emu68 (no devicetree.resource); nothing to do")
    except AmigaError as e:
        if "Emu68" in str(e):
            raise
        # an amigentd before 1.4.0 has no LOADED: the card's own config.txt has to tell
    ctl = _session().ctl
    if volume:
        return emu68card.AmigaCard(ctl, volume, _with_ctl)
    for v in _with_ctl(lambda: ctl.volumes()):
        card = emu68card.AmigaCard(ctl, v["name"], _with_ctl)
        try:
            text = card.read("config.txt") if (card.size("config.txt") or 0) < 65536 else b""
        except AmigaError:
            continue
        if b"emu68" in text.lower():
            return card
    raise AmigaError("no volume with an Emu68 config.txt; the boot partition is not mounted on the "
                     "Amiga (it needs a FAT file system, and to be written, sd.unit0=rw or emmc.unit0=rw), "
                     "or name it with volume='BOOT:'")


def _emu68_root():
    from . import emu68card, paths
    return emu68card.default_root() / paths.safe(_session().host)


@mcp.tool()
def amiga_emu68_card(check: bool = False, write_test: bool = False, volume: str = "") -> str:
    """Emu68's boot partition on the PiStorm's SD card, read only: kernel and its
    Emu68 version, board variant, Kickstart file (size, checksum), FPGA files
    named in config.txt (never written), every cmdline.txt option explained,
    overlays, free space, backups, problems such as a missing file, and whether
    changes are allowed (the user's marker file amigent-write-ok.txt on the card).

    check=True reads every file once on the Amiga (read errors, read speed);
    write_test=True also writes, reads back and deletes a 256 KB test file, only
    on a card that allows changes. volume: the boot partition, found by itself.
    Says what sd.unit0=rw means when it is set. Does nothing without Emu68.
    """
    from . import emu68card
    card = _emu68_card(volume)
    return emu68card.overview(card, check=check, write_test=write_test)


@mcp.tool()
def amiga_emu68_option(name: str = "", value: str = "", remove: bool = False, force: bool = False,
                       confirm: bool = False, volume: str = "") -> str:
    """Change one Emu68 option in cmdline.txt on the SD card, with a way back.

    name and value as in cmdline.txt (vc4.mem, 32; nofpu without a value);
    remove=True takes the option out. The option must be one Emu68 knows and the
    value valid; force=True writes an unknown option anyway. Without name: the
    list of known options and their values.

    A dry run that shows the plan unless confirm=True, and only on a card with
    the user's marker file amigent-write-ok.txt. Before writing, cmdline.txt goes
    into a backup on the card and on the PC; after writing it is read back. Takes
    effect at the next start; never restarts by itself. amiga_emu68_undo takes it back.
    Writing is untested on real hardware: try it on a second SD card first.
    """
    from . import emu68card
    if not name.strip():
        lines = ["Emu68 options in cmdline.txt (Emu68 1.0; 1.1 takes most from overlays in config.txt):"]
        for o in emu68card.OPTIONS.values():
            values = "" if o.kind == "flag" else "=" + emu68card._values(o)
            lines.append(f"  {o.name}{values}: {o.text}")
        return "\n".join(lines)
    card = _emu68_card(volume)
    return emu68card.change_option(card, _emu68_root(), name.strip(), value.strip() or None,
                                   remove=remove, force=force, confirm=confirm)


@mcp.tool()
def amiga_emu68_kickstart(file: str, confirm: bool = False, volume: str = "") -> str:
    """Boot another Kickstart: changes the ROM name in config.txt's initramfs line.

    file: a ROM file on the card (its name, such as kick31.rom), or an Amiga
    path (DEVS:Kickstarts/kick.rom) which is copied onto the card first. The ROM
    is checked first: size 256 KB to 2 MB, a Kickstart header, not byte swapped
    or encrypted, the checksum right (or checksum_rom set). The old ROM stays on
    the card, so going back is this call with its name, or amiga_emu68_undo.

    A dry run unless confirm=True, only on a card with the marker file
    amigent-write-ok.txt, with a backup on the card and the PC and a read back.
    Takes effect at the next start. Untested on real hardware: try a second card first.
    """
    from . import emu68card
    card = _emu68_card(volume)
    file = file.strip()
    if ":" in file:
        data = _with_ctl(lambda: _session().ctl.read_file(file))
        name = file.split(":", 1)[1].rsplit("/", 1)[-1]
        return emu68card.change_kickstart(card, _emu68_root(), name, data, confirm=confirm)
    return emu68card.change_kickstart(card, _emu68_root(), file, confirm=confirm)


@mcp.tool()
def amiga_emu68_undo(backup: str = "", confirm: bool = False, volume: str = "") -> str:
    """Put the SD card's boot files back as one backup has them: the last change by
    default, or the backup named (as amiga_emu68_card or an earlier answer gives it).

    Only the files of that backup are written, each checked against its checksum
    first; the present state is backed up before. A dry run unless confirm=True,
    and only on a card with the marker file amigent-write-ok.txt.
    """
    from . import emu68card
    card = _emu68_card(volume)
    return emu68card.undo(card, _emu68_root(), backup.strip(), confirm=confirm)


def _videocore_version():
    for path in ("LIBS:Picasso96/VideoCore.card", "LIBS:Picasso96/emu68-vc4.card"):
        try:
            result = _with_ctl(lambda: _session().ctl.exec(f"Version FILE {path}"))
        except AmigaError:
            continue
        m = re.search(r"(\d+)\.(\d+)", result.output)
        if result.rc == 0 and m:
            return int(m.group(1)), int(m.group(2))
    return None


@mcp.tool()
def amiga_emu68_update(archive: str = "", confirm: bool = False, after_restart: bool = False,
                       volume: str = "") -> str:
    """Update Emu68 on the SD card, with a backup of the whole boot partition.

    archive: an Emu68 release on this PC, the zip from github.com/michalsc/Emu68
    or its unpacked folder. Checks first and stops if something does not fit:
    the version on the card against the new one, the board variant (PiStorm32-
    lite/16 or the CPLD PiStorm), VideoCore.card in LIBS:Picasso96 against what
    the release needs (1.1 needs 1.5, 1.0 does not work with 1.5).

    With confirm=True: backs up the whole boot partition on the card (with a
    README on how to go back) and on the PC, with checksums; writes the kernel
    and the Pi firmware files next to the old ones, verifies them, swaps them in
    and verifies again. cmdline.txt, the Kickstart, FPGA files and the Amiga
    partitions are never written; config.txt only in its kernel= line when the
    kernel's name changes. Then the user restarts (amiga_restart), and
    after_restart=True confirms that the new Emu68 runs.

    If the new Emu68 does not start, a power cycle does not help; the rescue is
    the card in a PC: python -m amigent.emu68card restore <backup> <card drive>.
    Only on a card with the user's marker file amigent-write-ok.txt, and only on
    the user's yes. Untested on real hardware: try it on a second SD card first.
    """
    from . import emu68card
    if after_restart:
        versions = {lib.name: (lib.version, lib.revision) for lib in _loaded().libraries}
        return emu68card.confirm_update(_emu68_root(), versions, running=_emu68_running())
    if not archive.strip():
        raise AmigaError("archive: the Emu68 release zip or folder on this PC")
    path = Path(archive.strip()).expanduser()
    if not path.exists():
        raise AmigaError(f"{path} is not on this PC")
    card = _emu68_card(volume)
    files = emu68card.read_archive(path)
    root = _emu68_root()
    root.mkdir(parents=True, exist_ok=True)
    return emu68card.update(card, files, root, videocore=_videocore_version(),
                            running=_emu68_running(), confirm=confirm)


def _emu68_running() -> str | None:
    """The version of the Emu68 that runs now, from amigentd 1.5.0's EMU68 answer; None
    when amigentd is older, so the card's kernel file stands in for it."""
    from . import emu68
    try:
        version = emu68.parse(_with_ctl(_session().ctl.emu68_info)).get("version", "")
    except (AmigaError, OSError, ValueError):
        return None
    return version if re.match(r"\d+\.\d+", version) else None


def _close_for_update() -> None:
    """Our own connections go, so amigent-setup --update has each Amiga to itself."""
    with sessions._lock:
        open_now = list(sessions._sessions.values())
    for session in open_now:
        try:
            with session.calls, bound(session):
                with _exclusive():
                    session.ctl.close()
                with _screen_lock():
                    session.screen.close()
        except Exception:  # noqa: BLE001 - nothing to close is fine
            pass


def main() -> None:
    update.refresh_in_background()
    mcp.run()


def _listen_seconds(seconds) -> float:
    """The clip length the tool accepts: a number from a fifth of a second to two."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise AmigaError("seconds must be a number from 0.2 to 2")
    value = float(seconds)
    if not 0.2 <= value <= 2.0:
        raise AmigaError("seconds must be a number from 0.2 to 2")
    return value


def _listen_clip(seconds: float) -> tuple[int, bytes]:
    """Gather up to seconds of the Apollo mix. Opcode 48, not LOADED."""
    from .channel import pcm_wav
    deadline = time.monotonic() + max(seconds, 0.4) * 12
    chunks: list[bytes] = []
    rate = 0
    last = b""
    want = int(seconds * 48000 * 4)
    while time.monotonic() < deadline and sum(len(c) for c in chunks) < want:
        got_rate, pcm = _with_ctl(lambda: _session().ctl.audio(200))
        if pcm and pcm != last:
            if rate and got_rate != rate:
                break
            rate = got_rate or rate
            last = pcm
            chunks.append(pcm)
            if rate:
                want = int(seconds * rate * 4)
            continue
        time.sleep(0.002)
    pcm = b"".join(chunks)
    return rate, pcm_wav(pcm, rate) if pcm and rate else b""


def _audio_result(wav: bytes):
    """An MCP audio block, from whichever module this sdk version exports it."""
    try:
        from mcp.server.fastmcp import Audio as McpAudio
    except ImportError:
        from mcp.server.fastmcp.utilities.types import Audio as McpAudio
    return McpAudio(data=wav, format="wav")


@mcp.tool()
def amiga_listen(seconds: float = 1.0) -> list:
    """Hear what an AHI program is playing on ApolloOS.

    A short clip of the mix arne.audio is sending to the DAC. Nothing comes
    back when the Amiga is quiet, or when the sound is not going through AHI.
    seconds is from 0.2 to 2. This is opcode 48, not LOADED. The returned
    count is bytes received from the daemon, not a claim that they were heard.
    """
    seconds = _listen_seconds(seconds)
    info = _with_ctl(_session().ctl.sysinfo)
    if info.get("audio") != "1":
        return ["this Amiga has no AHI mix to hear"]
    rate, wav = _listen_clip(seconds)
    if not wav:
        return ["nothing is playing through AHI"]
    return [f"heard {len(wav) - 44} bytes at {rate} Hz", _audio_result(wav)]

if __name__ == "__main__":
    main()
