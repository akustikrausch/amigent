"""Machine-bound MCP state. A request never changes another request's target."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import os
import threading

from . import locks, machines, pack, watch
from .amigentd import Amigentd
from .channel import Commands
from .errors import AmigaError


class MachineSession:
    def __init__(self, host, ctl_port=6800, screen_port=6801):
        self.host = host
        self.address = machines.address(host)
        self.ctl = Commands(host, ctl_port, screen_port)
        self.screen = Amigentd(host, screen_port)
        self.lock_file = 'amigent-' + hashlib.sha256(host.encode()).hexdigest()[:24] + '.lock'
        self.calls = threading.RLock()
        self.others_seen = {'at': -1e9, 'text': '', 'told': ''}
        self.outdated_told = False
        self.system_seen = {}
        self.last_started = ''
        self.windows_at_start = (0.0, [])
        self.last_shot = None
        self.lookout = watch.Lookout()
        self.viewer = None
        self.last_look = 0.0
        self.debug_seen = 0
        self.followers = {}
        self.job_numbers = []
        self.self_path = {}
        self.gdb = {}


    def refresh_address(self):
        """An explicit registry edit must not leave commands on the old socket."""
        address = machines.address(self.host)
        if address == self.address:
            return
        if self.viewer:
            self.viewer.retire()
            self.viewer = None
        for follower in self.followers.values():
            follower.stop()
        self.followers.clear()
        with locks.machine(self.lock_file):
            self.ctl.close()
        with locks.machine(self.lock_file, screen=True):
            self.screen.close()
        self.job_numbers.clear()
        self.self_path.clear()
        if conn := self.gdb.pop("conn", None):
            conn.close()
            self.gdb.pop("where", None)
        self.system_seen.clear()
        self.others_seen.update(at=-1e9, text='', told='')
        self.last_shot = None
        self.last_look = 0.0
        self.debug_seen = 0
        self.windows_at_start = (0.0, [])
        self.last_started = ''
        self.lookout.baseline = 0.0
        self.address = address


active: ContextVar[MachineSession | None] = ContextVar('amigent_machine', default=None)


def current():
    session = active.get()
    if session is None:
        raise AmigaError('Select a machine for this call; use amiga_machines to list them.')
    return session


@contextmanager
def bound(session):
    token = active.set(session)
    try:
        yield session
    finally:
        active.reset(token)


def bind_callback(fn):
    """Capture identity for callbacks that outlive the request or run in threads."""
    session = current()
    @wraps(fn)
    def run(*args, **kwargs):
        with bound(session):
            return fn(*args, **kwargs)
    return run


class Sessions:
    def __init__(self, ctl_port=6800, screen_port=6801):
        self.ctl_port, self.screen_port = ctl_port, screen_port
        self.configured = os.environ.get('AMIGENT_HOST', '')
        try:
            self.configured = machines.identity(self.configured)
        except machines.RegistryError:
            # Discovery must still work. resolve() reports registry errors
            # at call time and retries after the file has been repaired.
            pass
        self._sessions = {}
        self._lock = threading.Lock()

    def resolve(self, machine=''):
        entries = machines.load()
        target = machine or self.configured
        if not target:
            defaults = [e for e in entries if e.get('default')]
            choices = defaults or entries
            if len(choices) != 1:
                raise AmigaError('Specify machine by saved name or ID; use amiga_machines to list them.')
            target = choices[0]['id']
        entry = machines.find(target)
        if not entry and (machine or not self.configured):
            raise AmigaError(f'No saved Amiga named {target!r}; use amiga_machines to list them.')
        host = entry['id'] if entry else target
        address = machines.address(host)  # validate removal and conflicts before any work
        with self._lock:
            session = self._sessions.get(host)
            if session is None:
                session = MachineSession(host, self.ctl_port, self.screen_port)
                pack.policy_for(host, mode=entry.get('compress') if entry else None)
                self._sessions[host] = session
            name = entry['name'] if entry else target
            label = f"{name} ({address})" if machine and len(entries) > 1 else ""
            return session, label
