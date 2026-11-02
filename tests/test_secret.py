from amigent import secret


def test_each_amiga_keeps_its_own_key(tmp_path, monkeypatch):
    monkeypatch.setattr(secret, "FOLDER", tmp_path)
    monkeypatch.delenv("AMIGENT_KEY", raising=False)
    secret.save_key("first", "10.0.0.1")
    secret.save_key("second", "10.0.0.2")
    assert secret.load_key("10.0.0.1") == "first"
    assert secret.load_key("10.0.0.2") == "second"
    assert secret.own_key("10.0.0.3") is None
    assert secret.load_key("amiga1200") == "first"  # the first key serves other names
    assert (tmp_path / "key-10.0.0.2").stat().st_mode & 0o777 == 0o600


def test_host_names_stay_inside_the_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(secret, "FOLDER", tmp_path)
    path = secret.save_key("k", "../x/fe80::1")
    assert path.parent == tmp_path and ":" not in path.name and "/" not in path.name


def test_a_name_finds_the_key_of_its_address(tmp_path, monkeypatch):
    monkeypatch.setattr(secret, "FOLDER", tmp_path)
    monkeypatch.delenv("AMIGENT_KEY", raising=False)
    secret.save_key("first", "10.0.0.1")
    secret.save_key("second", "127.0.0.1")
    assert secret.load_key("localhost") == "second"
