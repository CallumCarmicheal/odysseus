import importlib
import json
import sys


def _import_vault_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("ODYSSEUS_VAULT_FILE", str(tmp_path / "vault.json"))
    sys.modules.pop("core.database", None)
    sys.modules.pop("src.vault_config", None)
    sys.modules.pop("src.secret_storage", None)
    from src import secret_storage

    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    return importlib.import_module("src.vault_config")


def _stored_vault_value():
    from core.database import AppSetting, SessionLocal

    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == "vault_config").one()
        return dict(row.value or {})
    finally:
        db.close()


def test_vault_config_saves_encrypted_session_to_database(tmp_path, monkeypatch):
    vault_config = _import_vault_config(tmp_path, monkeypatch)

    vault_config.save_vault_config({
        "server_url": "https://vault.example",
        "email": "user@example.com",
        "session": "bw-session",
    })

    assert not (tmp_path / "vault.json").exists()
    raw = _stored_vault_value()
    assert raw["session"].startswith("enc:")
    assert "bw-session" not in json.dumps(raw)
    assert vault_config.load_vault_config()["session"] == "bw-session"


def test_vault_config_imports_legacy_json(tmp_path, monkeypatch):
    (tmp_path / "vault.json").write_text(
        json.dumps({
            "server_url": "https://vault.example",
            "email": "user@example.com",
            "session": "legacy-session",
        }),
        encoding="utf-8",
    )
    vault_config = _import_vault_config(tmp_path, monkeypatch)

    loaded = vault_config.load_vault_config()

    assert loaded["session"] == "legacy-session"
    raw = _stored_vault_value()
    assert raw["session"].startswith("enc:")
    migrated = json.loads((tmp_path / "vault.json").read_text(encoding="utf-8"))
    assert migrated["session"].startswith("enc:")
    assert "legacy-session" not in json.dumps(migrated)
