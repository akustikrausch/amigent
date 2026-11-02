import pytest


@pytest.fixture(autouse=True)
def private_config(tmp_path, monkeypatch):
    """Every test gets an empty config folder, and never searches the real network."""
    from amigent import machines, secret, tls

    folder = tmp_path / "config"
    monkeypatch.setattr(secret, "FOLDER", folder)
    monkeypatch.setattr(tls, "FOLDER", folder)
    monkeypatch.setattr(machines, "scan", lambda: [])
    monkeypatch.delenv("AMIGENT_HOST", raising=False)
    machines._searched.clear()
    return folder
