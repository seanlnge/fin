from fin import config


def test_environments_have_separate_default_databases(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.delenv("FIN_DB_PATH", raising=False)
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    sandbox = config.load_config()
    monkeypatch.setenv("PLAID_ENV", "production")
    production = config.load_config()
    assert sandbox.db_path != production.db_path
    assert sandbox.db_path.name == "sandbox.db"
    assert production.db_path.name == "fin.db"


def test_config_repr_does_not_contain_secrets(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    monkeypatch.setenv("PLAID_CLIENT_ID", "test-client-private")
    monkeypatch.setenv("PLAID_SECRET", "test-secret-private")
    assert "test-client-private" not in repr(config.load_config())
    assert "test-secret-private" not in repr(config.load_config())


def test_explicit_env_file_anchors_relative_database(tmp_path, monkeypatch):
    env_file = tmp_path / "project" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("PLAID_ENV=sandbox\nFIN_DB_PATH=data/fin.db\n", encoding="utf-8")
    monkeypatch.delenv("FIN_DB_PATH", raising=False)
    monkeypatch.delenv("PLAID_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    loaded = config.load_config(env_file)
    assert loaded.db_path == env_file.parent / "data" / "fin.db"
