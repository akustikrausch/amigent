"""Other remote control and AI agent software on the same Amiga.

Two agents driving one Amiga at the same time move the same mouse pointer and
type on the same keyboard, and neither knows about the other. Nothing here
removes or stops anything: it finds what runs and says so, so the agent (and
the user) can see why a click went astray.

amigentd's SYSINFO lists the public message ports and the tasks (ports=,
tasks=, from 1.3.0); the table below says which names belong to whom. Being
installed (a file, a line in S:User-Startup) is only reported; a warning is for
what actually runs.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Agent:
    name: str
    what: str
    ports: tuple[str, ...] = ()   # public message ports (exact, any case)
    tasks: tuple[str, ...] = ()   # task names (exact, any case)
    tcp: tuple[int, ...] = ()     # ports it listens on
    files: tuple[str, ...] = ()   # where it usually lives
    startup: str = ""             # a word on its line in S:User-Startup


KNOWN = (
    Agent("amiagent", "the Amiga half of amimcp, another MCP bridge for AI agents",
          ports=("AMIAGENT",), tasks=("amiagent", "amiagent.020"), tcp=(7846,),
          files=("C:amiagent", "C:amiagent.020", "SYS:WBStartup/amiagent"), startup="amiagent"),
    Agent("amimon", "amimcp's status window", tasks=("amimon", "amimon.020", "amimon-mui",
                                                      "amimon-mui.020")),
)

WARNING = ("{names} also runs on this Amiga. If another AI agent or person drives the Amiga "
           "through it at the same time, both move the same mouse and type on the same "
           "keyboard: clicks can land elsewhere and typing can mix. Nothing was changed; ask "
           "the user before input-heavy work, or whether the other one is in use.")


@dataclass
class Found:
    agent: Agent
    running: bool = False
    how: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"name": self.agent.name, "what": self.agent.what, "running": self.running,
                "seen": self.how}


def names(field_value: str) -> set[str]:
    return {n.strip().lower() for n in (field_value or "").split("|") if n.strip()}


def from_sysinfo(info: dict) -> list[Found]:
    """What runs, by the port and task names amigentd reported."""
    ports, tasks = names(info.get("ports", "")), names(info.get("tasks", ""))
    found = []
    for agent in KNOWN:
        how = [f"port {p}" for p in agent.ports if p.lower() in ports]
        how += [f"task {t}" for t in agent.tasks if t.lower() in tasks]
        if how:
            found.append(Found(agent, True, how))
    return found


def listening(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan(ctl, host: str, info: dict) -> list[Found]:
    """Running ones from SYSINFO, plus the network and what is installed."""
    found = {f.agent.name: f for f in from_sysinfo(info)}
    for agent in KNOWN:
        f = found.get(agent.name) or Found(agent)
        for port in agent.tcp:
            if host and listening(host, port):
                f.running = True
                f.how.append(f"listens on TCP {port}")
        for path in agent.files:
            try:
                if ctl.exec(f'List >NIL: "{path}"', timeout=10).rc == 0:
                    f.how.append(f"installed as {path}")
            except Exception:  # noqa: BLE001 - a look around, never a failure
                pass
        if agent.startup:
            try:
                r = ctl.exec(f"Search >NIL: S:User-Startup {agent.startup} QUIET", timeout=10)
                if r.rc == 0:
                    f.how.append("started from S:User-Startup")
            except Exception:  # noqa: BLE001
                pass
        if f.how:
            found[agent.name] = f
    return list(found.values())


def warning(found: list[Found]) -> str:
    running = [f.agent.name for f in found if f.running]
    if not running:
        return ""
    return WARNING.format(names=" and ".join(running))
