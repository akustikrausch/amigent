"""Machine identity and management must not reroute other saved Amigas."""
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request
from unittest.mock import Mock

import pytest

from amigent import fleet, install, machines, notes, paths, roster, secret, tls
from amigent.viewer import Viewer


def legacy(folder, rows):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "amigas.json").write_text(json.dumps(rows))


def test_legacy_migration_preserves_identity_credentials_and_history(private_config):
    rows = [{"name": "Amiga", "host": "192.0.2.17", "default": True, "seen": ["192.0.2.17"]}]
    legacy(private_config, rows)
    (private_config / "key-Amiga").write_text("private-test-key")
    (private_config / "tls-Amiga.pem").write_text("test-cert")
    (private_config / "notes").mkdir()
    (private_config / "notes/Amiga.json").write_text('{"clock": []}')
    (private_config / "installs/Amiga").mkdir(parents=True)
    (private_config / "installs/Amiga/monitor.json").write_text('{"done": true}')
    (private_config / "inventory/Amiga").mkdir(parents=True)
    (private_config / "inventory/Amiga/scan.json").write_text('{}')
    (private_config / "uptime.json").write_text('{"Amiga": {"uptime": 123}}')
    entry = roster.load()[0]
    ident = entry["id"]
    assert roster.is_id(ident)
    assert json.loads((private_config / "amigas.pre-id.json").read_text()) == rows
    roster.rename(ident, "My A3000")
    assert roster.find("My A3000")["id"] == ident == roster.identity("Amiga")
    assert secret.load_key(ident) == "private-test-key"
    assert tls.pinned(ident).read_text() == "test-cert"
    assert notes.for_amiga("My A3000").path == private_config / f"notes/{ident}.json"
    assert (install.folder_for("My A3000") / "monitor.json").exists()
    assert (private_config / f"inventory/{ident}/scan.json").exists()
    assert json.loads((private_config / "uptime.json").read_text())[ident]["uptime"] == 123
    assert (private_config / "key-Amiga").exists()  # recovery copy remains


def test_migration_failure_does_not_destroy_legacy_registry(private_config, monkeypatch):
    rows = [{"name": "Old", "host": "192.0.2.1"}]
    legacy(private_config, rows)
    (private_config / "key-Old").write_text("test-key")
    original = roster.shutil.copy2
    monkeypatch.setattr(roster.shutil, "copy2", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        roster.load()
    assert json.loads((private_config / "amigas.json").read_text()) == rows
    assert (private_config / "key-Old").read_text() == "test-key"
    monkeypatch.setattr(roster.shutil, "copy2", original)
    assert secret.load_key(roster.load()[0]["id"]) == "test-key"


def test_setup_registration_cannot_replace_existing_machine(private_config):
    first = roster.remember("Amiga", "192.0.2.17")
    before = roster.list_file().read_bytes()
    with pytest.raises(roster.RegistryError, match="Edit address"):
        roster.remember("Amiga", "192.0.2.129")
    assert roster.list_file().read_bytes() == before
    second = roster.remember("Amiga 2", "192.0.2.129")
    assert first["id"] != second["id"]
    with pytest.raises(roster.RegistryError, match="already assigned"):
        roster.edit(first["id"], address=second["host"])
    assert roster.address(first["id"]) == "192.0.2.17"


def test_reused_ip_is_literal_and_current_owner_wins(private_config):
    first = roster.remember("First", "192.0.2.1")
    roster.edit(first["id"], address="192.0.2.2")
    assert roster.address("192.0.2.1") == "192.0.2.1"
    second = roster.remember("Second", "192.0.2.1")
    assert roster.find("192.0.2.1")["id"] == second["id"]
    assert roster.address("First") == "192.0.2.2"


def test_duplicates_are_visible_and_repairable_without_sharing_credentials(private_config):
    legacy(private_config, [{"name": "Amiga", "host": "192.0.2.129", "default": True},
                            {"name": "Amiga 2", "host": "192.0.2.129"}])
    (private_config / "key-192.0.2.129").write_text("ambiguous-key")
    entries = roster.load()
    with pytest.raises(roster.RegistryError, match="Ambiguous"):
        roster.find("192.0.2.129")
    with pytest.raises(roster.RegistryError, match="also assigned"):
        roster.address(entries[0]["id"])
    assert secret.load_key(entries[0]["id"]) is None
    overview = fleet.Fleet(entries)
    assert all(row["conflicts"] for row in overview.status())
    overview.manage({"id": entries[0]["id"], "action": "address", "address": "192.0.2.17"})
    assert not any(row["conflicts"] for row in overview.status())
    assert roster.address(entries[1]["id"]) == "192.0.2.129"


def test_rename_does_not_rebind_existing_clients(private_config):
    old = roster.remember("Amiga 2", "192.0.2.129")
    v = Viewer("Amiga 2")
    roster.rename(old["id"], "A3000")
    assert v.host == v.screen.host == v.ctl.host == old["id"]
    assert roster.address(v.host) == "192.0.2.129"
    assert roster.name_of(v.host) == "A3000"


def test_connection_requests_leave_registry_unchanged(private_config, monkeypatch):
    a = roster.remember("Amiga", "192.0.2.17")
    b = roster.remember("Amiga 2", "192.0.2.129")
    manager = fleet.Fleet(roster.load())
    monkeypatch.setattr(Viewer, "start", lambda self, port: "viewer:" + self.host)
    before = roster.list_file().read_bytes()
    assert manager.connect("Amiga 2") == "viewer:" + b["id"]
    assert manager.connect("192.0.2.200") == "viewer:192.0.2.200"
    assert roster.list_file().read_bytes() == before
    assert roster.address(a["id"]) == "192.0.2.17"


def test_remove_and_default_are_by_id_and_keep_history(private_config):
    a = roster.remember("A", "192.0.2.1")
    b = roster.remember("B", "192.0.2.2")
    secret.save_key("test", b["id"])
    manager = fleet.Fleet(roster.load())
    manager.manage({"id": b["id"], "action": "default"})
    assert roster.default() == "B"
    manager.manage({"id": b["id"], "action": "remove"})
    assert roster.default() == "A"
    assert (private_config / f"key-{b['id']}").read_text().strip() == "test"
    with pytest.raises(roster.RegistryError, match="removed"):
        roster.address(b["id"])
    assert [r["id"] for r in manager.status()] == [a["id"]]


def test_management_http_requires_token_and_survives_invalid_requests(private_config, monkeypatch):
    a = roster.remember("A", "192.0.2.1")
    monkeypatch.setattr(fleet.Fleet, "_watch", lambda *args: None)
    manager = fleet.Fleet(roster.load())
    manager.start(0)
    def post(body, token=manager.token):
        request = urllib.request.Request(f"http://127.0.0.1:{manager.port}/api/machines?t={token}",
                                         data=json.dumps(body).encode())
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    try:
        assert post({"id": a["id"], "action": "remove"}, "bad")[0] == 403
        assert post([])[0] == 400
        assert post({"id": "missing", "action": "remove"})[0] == 400
        assert post({"id": a["id"], "action": "rename", "name": "Renamed"})[0] == 200
        assert roster.find("Renamed")["id"] == a["id"]
    finally:
        manager.httpd.shutdown()
        manager.httpd.server_close()


def test_typing_even_a_known_ip_cannot_trigger_registry_relocation(private_config, monkeypatch):
    a = roster.remember("A", "192.0.2.17")
    manager = fleet.Fleet(roster.load())
    monkeypatch.setattr(Viewer, "start", lambda self, port: "viewer:" + self.host)
    manager.connect("192.0.2.17")
    direct = manager.direct["192.0.2.17"]
    assert isinstance(direct.host, roster.DirectAddress)
    assert isinstance(direct.screen.host, roster.DirectAddress)
    monkeypatch.setattr(tls, "_dial", Mock(side_effect=OSError("offline")))
    relocate = Mock()
    monkeypatch.setattr(machines, "relocate", relocate)
    with pytest.raises(OSError, match="offline"):
        tls.connect(direct.host, 6801, 1)
    relocate.assert_not_called()
    assert roster.address(a["id"]) == "192.0.2.17"


def test_current_ip_owner_gets_its_key_after_duplicate_repair(private_config):
    legacy(private_config, [{"name": "A", "host": "192.0.2.129", "seen": ["192.0.2.17", "192.0.2.129"]},
                            {"name": "B", "host": "192.0.2.129"}])
    (private_config / "key-192.0.2.17").write_text("first")
    (private_config / "key-192.0.2.129").write_text("second")
    a, b = roster.load()
    assert secret.load_key(a["id"]) == "first"
    roster.edit(a["id"], address="192.0.2.17")
    assert secret.load_key(b["id"]) == "second"
    roster.rename(b["id"], "Renamed B")
    assert (private_config / f"key-{b['id']}").read_text() == "second"


def test_migration_resumes_partially_copied_history(private_config, monkeypatch):
    legacy(private_config, [{"name": "Old", "host": "192.0.2.17"}])
    history = private_config / "installs/Old"
    history.mkdir(parents=True)
    for name in ("one", "two"):
        (history / name).write_text(name)
    copy = roster.shutil.copy2
    calls = 0
    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            Path(target).write_text("partial")
            raise OSError("disk full")
        return copy(source, target)
    monkeypatch.setattr(roster.shutil, "copy2", fail_second)
    with pytest.raises(OSError, match="disk full"):
        roster.load()
    monkeypatch.setattr(roster.shutil, "copy2", copy)
    ident = roster.load()[0]["id"]
    for name in ("one", "two"):
        assert (private_config / "installs" / ident / name).read_text() == name


def test_reusing_removed_name_does_not_adopt_old_credentials_or_history(private_config):
    legacy(private_config, [{"name": "Old", "host": "192.0.2.17"}])
    (private_config / "key-Old").write_text("old-key")
    (private_config / "notes").mkdir()
    (private_config / "notes/Old.json").write_text('{"old": []}')
    old = roster.load()[0]
    roster.forget(old["id"])
    new = roster.remember("Old", "192.0.2.129")
    roster.edit(new["id"], name="New")
    assert old["id"] != new["id"]
    assert secret.load_key(new["id"]) is None
    assert not (private_config / f"notes/{new['id']}.json").exists()
    assert (private_config / f"notes/{old['id']}.json").exists()


def test_rediscovery_without_credentials_cannot_reassign_saved_machine(private_config, monkeypatch):
    a = roster.remember("A", "192.0.2.17")
    monkeypatch.setattr(machines, "scan", lambda: ["192.0.2.129"])
    assert machines.relocate(a["id"]) is None
    assert roster.address(a["id"]) == "192.0.2.17"


@pytest.mark.parametrize("target,overview", [(None, True), ("B", False), ("192.0.2.17", False)])
def test_viewer_startup_selects_overview_or_explicit_target(private_config, monkeypatch, target, overview):
    import sys
    from amigent import viewer
    roster.remember("A", "192.0.2.17")
    b = roster.remember("B", "192.0.2.129")
    starts = []
    monkeypatch.setattr(viewer, "running_viewer", lambda host: None)
    monkeypatch.setattr(viewer.time, "sleep", Mock(side_effect=KeyboardInterrupt))
    monkeypatch.setattr(fleet.Fleet, "start", lambda self, port: starts.append(("overview", None)) or "test")
    monkeypatch.setattr(Viewer, "start", lambda self, port: starts.append(("viewer", self.host)) or "test")
    monkeypatch.setattr(sys, "argv", ["amigent-viewer", "--no-window", *([target] if target else [])])
    viewer.main()
    assert starts[0][0] == ("overview" if overview else "viewer")
    if target == "B":
        assert starts[0][1] == b["id"]
    elif target:
        assert isinstance(starts[0][1], roster.DirectAddress)


def test_explicit_address_edit_retires_old_viewer(private_config, monkeypatch):
    a = roster.remember("A", "192.0.2.17")
    monkeypatch.setenv("AMIGENT_SCREEN_PORT", "12345")
    manager = fleet.Fleet(roster.load())
    monkeypatch.setattr(Viewer, "start", lambda self, port: "test")
    manager.open_viewer(a["id"])
    old = manager.entry(a["id"])["viewer"]
    assert old.screen.port == 12345
    manager.manage({"id": a["id"], "action": "address", "address": "192.0.2.129"})
    assert not old.enabled
    assert manager.entry(a["id"])["viewer"] is None
    manager.open_viewer(a["id"])
    assert manager.entry(a["id"])["viewer"] is not old


def test_equivalent_ipv6_addresses_are_duplicates(private_config):
    legacy(private_config, [{"name": "A", "host": "2001:db8::1"},
                            {"name": "B", "host": "2001:0db8:0:0:0:0:0:1"}])
    assert all(roster.conflicts(a) for a in roster.load())
    with pytest.raises(roster.RegistryError, match="Ambiguous"):
        roster.find("2001:db8::1")


@pytest.mark.parametrize("already_migrated", [False, True])
def test_common_key_and_state_upgrade(private_config, already_migrated):
    from amigent import journal, states
    rows = [{"name": "Workbench", "host": "192.0.2.1"}]
    legacy(private_config, rows)
    (private_config / "key").write_text("sole machine")
    (private_config / "link.json").write_text('{"Workbench": {"mode": "best", "kbs": 42}}')
    for folder in ("screens", "checkpoints"):
        path = private_config / folder / "Workbench"
        path.mkdir(parents=True)
        (path / "sample").write_bytes(b"history")
    diary = journal.Journal()
    diary.add("Workbench", "version")
    store = states.Store(private_config / "states")
    store.save("Workbench", "before-test", {"at": 42})
    if already_migrated:
        (private_config / "amigas.pre-id.json").write_text(json.dumps(rows))
        legacy(private_config, [dict(rows[0], name="Renamed", previous_names=["Workbench"],
                                     id=roster._legacy_id("Workbench"), legacy_credentials=True)])
    ident = roster.load()[0]["id"]
    assert diary.recent(ident)[0]["did"] == "version"
    assert store.load(ident, "before-test") == {"at": 42}
    assert secret.load_key(ident) == "sole machine"
    assert json.loads((private_config / "link.json").read_text())[ident] == {"mode": "best", "kbs": 42}
    for folder in ("screens", "checkpoints"):
        assert (private_config / folder / ident / "sample").read_bytes() == b"history"
    other = roster.remember("Other", "192.0.2.2")
    assert secret.load_key(other["id"]) is None
    assert secret.load_key(roster.DirectAddress(other["host"])) is None


def test_shared_historical_pin_requires_verified_machine_pin(private_config, monkeypatch):
    from amigent.channel import Commands
    legacy(private_config, [{"name": "A", "host": "192.0.2.1", "seen": ["192.0.2.9"]},
                            {"name": "B", "host": "192.0.2.2", "seen": ["192.0.2.9"]}])
    (private_config / "tls-192.0.2.9.pem").write_text("shared")
    (private_config / "key").write_text("unassigned")
    a, b = roster.load()
    assert secret.load_key(a["id"]) is None
    monkeypatch.setattr(tls.socket, "create_connection", Mock(side_effect=AssertionError))
    for target in (a["id"], roster.DirectAddress(b["host"])):
        with pytest.raises(roster.RegistryError, match="Ambiguous TLS pin"):
            Commands(target).connect()
    roster.forget(b["id"])
    with pytest.raises(roster.RegistryError, match="verified certificate"):
        tls.wanted(a["id"])
    tls.remember(a["id"], b"verified")
    assert tls.pinned(a["id"]).read_bytes() == b"verified"


def test_pin_restored_after_migration_still_requires_proof(private_config):
    legacy(private_config, [{"name": "A", "host": "192.0.2.1", "seen": ["192.0.2.9"]},
                            {"name": "B", "host": "192.0.2.2", "seen": ["192.0.2.9"]}])
    a, b = roster.load()
    roster.forget(b["id"])
    assert not tls.wanted(a["id"])
    (private_config / "tls-192.0.2.9.pem").write_text("restored")
    with pytest.raises(roster.RegistryError, match="Ambiguous TLS pin"):
        tls.wanted(a["id"])


def test_invalid_registry_keeps_mcp_available(private_config):
    import asyncio
    import os
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config = private_config / "amigent"
    config.mkdir(parents=True)
    path = config / "amigas.json"
    path.write_bytes(b"{bad json")
    params = StdioServerParameters(command=sys.executable, args=["-m", "amigent.server"],
                                   env=dict(os.environ, XDG_CONFIG_HOME=str(private_config), AMIGENT_HOST="A"))

    async def check():
        async with stdio_client(params) as streams, ClientSession(*streams) as client:
            await client.initialize()
            assert (await client.list_tools()).tools
            reply = await client.call_tool("amiga_status", {})
            assert reply.isError and "amigas.json" in reply.content[0].text

    asyncio.run(check())
    assert path.read_bytes() == b"{bad json"


def test_overview_samples_without_an_open_desktop(private_config, monkeypatch):
    from PIL import Image
    from unittest.mock import patch
    fleet_state = fleet.Fleet([roster.remember("Test", "192.0.2.5")])
    entry = fleet_state.entries[0]
    facts = {"os": "3.2", "cpu": "68040", "fast_free": 8192}
    monkeypatch.setattr(fleet.net, "greet", lambda *a, **kw: b"AMGT")
    monkeypatch.setattr(entry["stop"], "wait", lambda timeout: entry["stop"].set())
    with patch("amigent.fleet.Viewer", wraps=Viewer) as make:
        fleet_state._watch(entry)
        make.assert_not_called()
    entry["stop"].clear()
    monkeypatch.setattr(Viewer, "read_amiga", lambda self: facts)
    monkeypatch.setattr("amigent.amigentd.Amigentd.capture", lambda self: Image.new("RGB", (32, 16)))
    fleet_state.overview_until = float("inf")
    fleet_state._watch(entry)
    row, = fleet_state.status()
    assert row["online"] and row["preview"] and not row["watching"]
    assert (row["os"], row["cpu"], row["free"]) == ("3.2", "68040", 8192)



def test_v14_state_prefix_collision_keeps_original(private_config):
    from amigent import states

    legacy(private_config, [{"name": "Workbench", "host": "192.0.2.1"},
                            {"name": "Workbench-Lab", "host": "192.0.2.2"}])
    store = states.Store(private_config / "states")
    store.save("Workbench-Lab", "before", {"at": 42})
    a, b = roster.load()
    assert store.load("Workbench-Lab", "before") == {"at": 42}
    assert store.load(a["id"], "Lab-before") == {}
    assert store.load(b["id"], "before") == {}


def test_previous_registry_upgrade_copies_released_journal_once(private_config):
    from amigent import journal

    legacy(private_config, [{"name": "Lab  Amiga", "host": "192.0.2.1"}])
    entry, = roster.load()
    entry.pop("v14_history_migrated")
    roster.save([entry])
    diary = journal.Journal()
    diary.add("Lab  Amiga", "first action")
    roster.load()
    roster.rename(entry["id"], "New name")
    diary.add(entry["id"], "second action")
    assert [item["did"] for item in diary.recent(entry["id"])] == ["first action", "second action"]
