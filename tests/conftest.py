import pytest


@pytest.fixture(autouse=True)
def private_config(tmp_path, monkeypatch):
    """Every test gets an empty config folder, and never searches the real network."""
    from amigent import machines, paths, secret, tls

    folder = tmp_path / "config"
    monkeypatch.setattr(paths, "FOLDER", folder)  # the roll call
    monkeypatch.setattr(secret, "FOLDER", folder)
    monkeypatch.setattr(tls, "FOLDER", folder)
    monkeypatch.setattr(machines, "scan", lambda: [])
    monkeypatch.delenv("AMIGENT_HOST", raising=False)
    machines._searched.clear()
    return folder


@pytest.fixture(autouse=True)
def machine_session(private_config, monkeypatch):
    from amigent import server
    from amigent.sessions import MachineSession, Sessions, bound
    monkeypatch.setattr(server, "sessions", Sessions())
    with bound(MachineSession("test")) as session:
        ctl, screen = session.ctl, session.screen
        yield session
        if ctl.backend is None or hasattr(ctl.backend, "close"):
            ctl.close()
        screen.close()
        session.lookout._pool.shutdown(wait=True)
