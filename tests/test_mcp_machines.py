"""MCP calls bind one machine, including nested calls and delayed callbacks."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
import threading
import zipfile

import pytest

from amigent import install, roster, server
from amigent.amigactl import ExecResult
from amigent.errors import AmigaError
from amigent.sessions import active, bind_callback, bound


@pytest.fixture
def two(monkeypatch):
    a = roster.remember('Amiga3000', '192.0.2.129')
    b = roster.remember('Amiga4000', '192.0.2.17')
    sessions = [server.sessions.resolve(x['id'])[0] for x in (a, b)]
    for session in sessions:
        session.ctl = MemoryCommands(session.host)
    token = active.set(None)
    yield a, b, *sessions
    active.reset(token)
    for session in sessions:
        session.lookout._pool.shutdown(wait=True)


class MemoryCommands:
    connected = True
    backend = None

    def __init__(self, host):
        self.host = host
        self.files = {}
        self.barrier = None
        self.fail = False

    def exec(self, command, cd='', timeout=30):
        if self.barrier:
            self.barrier.wait(3)
        if self.fail:
            raise AmigaError('offline', 1)
        return ExecResult(0, self.host)

    def close(self):
        pass

    def write_file(self, path, data):
        self.files[path] = data

    def delete(self, path):
        if self.files.pop(path, None) is None:
            raise AmigaError('object not found', 205)

    def list_dir(self, path):
        return []


def test_every_machine_tool_exposes_target_and_shared_tools_do_not(two):
    tools = asyncio.run(server.mcp.list_tools())
    for tool in tools:
        assert ('machine' in tool.inputSchema['properties']) == (tool.name not in server.UNTARGETED)
    assert len(tools) == 97


def test_actual_mcp_dispatch_runs_two_machines_concurrently(two):
    a, b, sa, sb = two
    sa.ctl.barrier = sb.ctl.barrier = threading.Barrier(2)
    async def both():
        return await asyncio.gather(*[
            server.mcp.call_tool('amiga_exec', {'machine': x['name'], 'command': 'Version'})
            for x in (a, b)])
    replies = asyncio.run(both())
    for reply, entry in zip(replies, (a, b)):
        text = str(reply)
        assert entry['name'] in text and entry['id'] in text
    assert active.get() is None


def test_omitted_unknown_and_conflicting_targets_do_not_execute(two):
    a, b, sa, sb = two
    assert a['id'] in server.amiga_exec('Version')
    roster.edit(b['id'], make_default=True)
    assert b['id'] in server.amiga_exec('Version')
    rows = roster.load()
    for entry in rows:
        entry.pop('default', None)
    roster.save(rows)
    with pytest.raises(AmigaError, match='Specify machine'):
        server.amiga_exec('Version')
    with pytest.raises(AmigaError, match='No saved Amiga'):
        server.amiga_exec('Version', machine='typo')
    rows = roster.load()
    rows[1]['host'] = rows[0]['host']
    roster.save(rows)
    with pytest.raises(AmigaError, match='also assigned'):
        server.amiga_exec('Version', machine=a['name'])


def test_failure_is_attributed_and_other_machine_still_works(two):
    a, b, sa, sb = two
    sa.ctl.fail = True
    with pytest.raises(AmigaError, match='Amiga3000.*offline'):
        server.amiga_exec('Version', machine=a['id'])
    assert b['id'] in server.amiga_exec('Version', machine=b['id'])
    assert active.get() is None


def test_nested_calls_and_background_callbacks_keep_identity(two):
    a, b, sa, sb = two
    callbacks = []
    for session in (sa, sb):
        with bound(session):
            callbacks.append(bind_callback(lambda: server.amiga_exec('Version')))
    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda fn: fn(), callbacks))
    assert sa.host in replies[0] and sb.host not in replies[0]
    assert sb.host in replies[1] and sa.host not in replies[1]
    assert sa.lock_file != sb.lock_file
    assert sa.lookout is not sb.lookout
    sa.debug_seen = 123
    sa.system_seen['cpu'] = '68030'
    assert sb.debug_seen == 0 and not sb.system_seen


def test_rename_preserves_session_and_notes(two):
    a, b, sa, sb = two
    server.amiga_note('test', 'only on first', machine=a['name'])
    roster.rename(a['id'], 'Renamed')
    assert server.sessions.resolve('Renamed')[0] is sa
    assert 'only on first' in server.amiga_notes('test', machine='Renamed')
    assert 'only on first' not in server.amiga_notes('test', machine=b['name'])


def test_listing_is_read_only_and_reports_conflicts(two, monkeypatch):
    a, b, *_ = two
    monkeypatch.setattr(server.net, 'greet', lambda host, *args, **kwargs: b'AMGT' if host == a['host'] else b'')
    before = roster.list_file().read_bytes()
    rows = json.loads(server.amiga_machines())
    assert [r['online'] for r in rows] == [True, False]
    assert roster.list_file().read_bytes() == before


def test_same_package_install_keeps_files_and_records_separate(two, monkeypatch):
    a, b, sa, sb = two
    monkeypatch.setattr(install, 'expand_map', lambda ctl, paths: (paths, {}))
    monkeypatch.setattr(install, 'snapshot', lambda ctl, path, crc=True: {})
    # the backup job: started, ended, complete (tests/test_install_backup.py runs real ones)
    monkeypatch.setattr(server, '_start_job', lambda script, title='': 1)
    monkeypatch.setattr(server, '_wait_job', lambda number, wait: '')
    monkeypatch.setattr(server, '_marker_ok', lambda record: True)
    monkeypatch.setattr(server.files, 'makedirs', lambda ctl, path: None)
    monkeypatch.setattr(server.aminet, 'tool_for', lambda name: None)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('example.txt', b'package payload')
    monkeypatch.setattr(server.aminet, 'download', lambda path: archive.getvalue())
    def put(ctl, local, target):
        ctl.write_file(target + '/example.txt', (local / 'example.txt').read_bytes())
    monkeypatch.setattr(server.files, 'put', put)
    def workflow(entry):
        name = entry['name']
        server.amiga_install_begin('example', folders='Work:Utilities', system=False, machine=name)
        result = server.amiga_aminet_install('util/misc/example.zip', 'Work:Utilities', machine=name)
        server.amiga_install_end('example', machine=name)
        return result
    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(workflow, (a, b)))
    for entry, session, reply in zip((a, b), (sa, sb), replies):
        assert entry['name'] in reply
        assert session.ctl.files['Work:Utilities/example.txt'] == b'package payload'
        assert install.load(entry['id'], 'example').done
    assert install.folder_for(a['id']) != install.folder_for(b['id'])


def test_single_machine_and_configured_default_remain_supported(two):
    a, b, *_ = two
    server.sessions.configured = b['id']
    assert b['id'] in server.amiga_exec('Version')
    assert a['id'] in server.amiga_exec('Version', machine=a['id'])
    server.sessions.configured = ''
    roster.forget(b['id'])
    assert a['id'] in server.amiga_exec('Version')


def test_power_command_cannot_operate_unrelated_machine(two, monkeypatch):
    a, b, *_ = two
    monkeypatch.setattr(server, 'power_available', lambda: True)
    server.sessions.configured = a['id']
    with pytest.raises(AmigaError, match='No power control bound'):
        server.amiga_power('off', machine=b['id'])


def test_address_change_closes_cached_channels_before_next_call(two):
    from unittest.mock import Mock
    a, b, sa, sb = two
    sa.ctl.close = Mock()
    sa.screen.close = Mock()
    sa.system_seen['cpu'] = 'cached'
    roster.edit(a['id'], address='192.0.2.130')
    reply = server.amiga_exec('Version', machine=a['id'])
    assert '192.0.2.130' in reply
    sa.ctl.close.assert_called_once()
    sa.screen.close.assert_called_once()
    assert not sa.system_seen
    assert sb.address == b['host']


def test_screen_poll_retains_target_after_call_times_out(two):
    from amigent.sessions import current
    a, b, sa, sb = two
    entered, release = threading.Event(), threading.Event()
    observed = []
    class Screen:
        def snapshot(self): return None
        def pause_left(self): return 0
        def poll(self, *args):
            entered.set()
            assert release.wait(3)
            observed.append(current().host)
        def changed_since(self, *args): return 0
    sa.screen = Screen()
    try:
        reply = server.amiga_wait_change(timeout=0.01, machine=a['id'])
        assert 'still on its way' in reply
        assert entered.wait(2)
        assert b['id'] in server.amiga_exec('Version', machine=b['id'])
    finally:
        release.set()
        if sa.lookout._busy:
            sa.lookout._busy.result(timeout=3)
    assert observed == [a['id']]


def test_parallel_link_settings_preserve_both_machines(two, private_config):
    from amigent.pack import Policy
    a, b, *_ = two
    path = private_config / 'link.json'
    policies = [Policy(e['id'], e['host'], path=path) for e in (a, b)]
    def save(policy):
        for _ in range(20):
            policy.set_mode('fast')
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(save, policies))
    assert set(json.loads(path.read_text())) == {a['id'], b['id']}



def test_single_machine_keeps_json_lists_and_errors(two, monkeypatch):
    a, b, sa, _ = two
    roster.forget(b["id"])
    monkeypatch.setattr(sa.ctl, "sysinfo", lambda: {}, raising=False)
    reply = asyncio.run(server.mcp.call_tool("amiga_link", {}))
    content, _ = reply
    assert json.loads(content[0].text)["mode"] == "auto"
    monkeypatch.setattr(server, "_paused_note", lambda: "screen paused")
    assert server.amiga_screenshot(machine=a["name"]) == ["screen paused"]
    with pytest.raises(TypeError, match="required positional"):
        server.amiga_exec()


@pytest.mark.parametrize("screen", [False, True])
def test_machine_locks_exclude_legacy_processes(two, tmp_path, monkeypatch, screen):
    import os
    import subprocess
    import sys
    from amigent import locks

    if os.name == "nt":
        pytest.skip("this probe uses POSIX nonblocking flock")
    _, _, sa, sb = two
    monkeypatch.setattr(locks, "path", lambda name: tmp_path / name)
    probe = """
import fcntl, sys
with open(sys.argv[1], "a+b") as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(1)
"""
    suffix = ".screen" if screen else ""
    def locked(filename):
        result = subprocess.run([sys.executable, "-c", probe, tmp_path / filename])
        assert result.returncode in (0, 1)
        return result.returncode == 1

    with locks.machine(sa.lock_file, screen=screen):
        with locks.machine(sb.lock_file, screen=screen):
            assert locked("amigent.lock" + suffix)
            assert locked(sa.lock_file + suffix)
            assert locked(sb.lock_file + suffix)
            assert not locked("amigent.lock" + ("" if screen else ".screen"))
    assert not locked("amigent.lock" + suffix)


def test_default_reply_is_json_with_two_registered_machines(two, monkeypatch):
    a, b, sa, sb = two
    monkeypatch.setattr(sa.ctl, "sysinfo", lambda: {}, raising=False)
    monkeypatch.setattr(sb.ctl, "sysinfo", lambda: {}, raising=False)
    reply = asyncio.run(server.mcp.call_tool("amiga_link", {}))
    content, _ = reply
    assert json.loads(content[0].text)["mode"] == "auto"
    explicit = server.amiga_link(machine=b["name"])
    assert explicit.startswith(f"{b['name']} ({b['host']}):")
    with pytest.raises(TypeError):
        server.amiga_exec()


def test_jobs_and_emulator_connections_belong_to_the_target(two, monkeypatch):
    a, b, sa, sb = two
    monkeypatch.setattr(server, "_known_jobs", lambda: [])
    monkeypatch.setenv("AMIGENT_GDB", "127.0.0.1:2345")
    server.sessions.configured = a["id"]
    monkeypatch.setattr(server.gdb, "Gdb", lambda endpoint: object())
    with bound(sa):
        assert server._next_job() == 1
        conn = server._emulator()
    with bound(sb):
        assert server._next_job() == 1
        with pytest.raises(AmigaError, match="no emulator debugger"):
            server._emulator()
        assert server._install_checkpoint("example") == ""
    rows = roster.load()
    rows[1]["gdb"] = "127.0.0.1:2346"
    roster.save(rows)
    with bound(sb):
        assert server._emulator() is not conn
    with bound(sa):
        assert server._next_job() == 2 and server._emulator() is conn


def test_everywhere_uses_both_sessions_without_selecting_a_target(two):
    a, b, sa, sb = two
    sa.ctl.barrier = sb.ctl.barrier = threading.Barrier(2)
    answer = server.amiga_everywhere("Version")
    assert "2 different results" in answer
    for entry in (a, b):
        assert entry["name"] in answer and entry["id"] in answer
    assert active.get() is None


def test_an_amiga_found_again_keeps_its_viewer_and_followers(two):
    """DHCP gave the Amiga a new address and the search proved it is the same one:
    only the sockets are stale. An address typed by the user still starts over."""
    from unittest.mock import Mock
    a, b, sa, sb = two
    sa.ctl.close, sa.screen.close = Mock(), Mock()
    sa.viewer, follower = Mock(), Mock()
    sa.followers['T:log'] = follower
    sa.system_seen['cpu'] = 'cached'
    roster.edit(a['id'], address='192.0.2.130', proven=True)
    assert roster.found_again(a['id'])
    assert '192.0.2.130' in server.amiga_exec('Version', machine=a['id'])
    sa.ctl.close.assert_called_once()
    sa.screen.close.assert_called_once()
    sa.viewer.retire.assert_not_called()
    follower.stop.assert_not_called()
    assert sa.system_seen == {'cpu': 'cached'} and sa.address == '192.0.2.130'
    viewer = sa.viewer
    roster.edit(a['id'], address='192.0.2.131')  # by hand: no longer proven
    assert not roster.found_again(a['id'])
    server.amiga_exec('Version', machine=a['id'])
    viewer.retire.assert_called_once()
    follower.stop.assert_called_once()
