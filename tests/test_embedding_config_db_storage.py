import importlib
import json
import sys


def _import_embedding_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("ODYSSEUS_EMBEDDING_ENDPOINT_FILE", str(tmp_path / "embedding_endpoint.json"))
    sys.modules.pop("core.database", None)
    sys.modules.pop("src.embedding_config", None)
    return importlib.import_module("src.embedding_config")


def _stored_embedding_value():
    from core.database import AppSetting, SessionLocal

    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == "embedding_endpoint").one_or_none()
        return dict(row.value or {}) if row is not None else None
    finally:
        db.close()


def test_embedding_endpoint_imports_legacy_json_to_database(tmp_path, monkeypatch):
    (tmp_path / "embedding_endpoint.json").write_text(
        json.dumps({"url": "http://localhost:11434/v1/embeddings", "model": "nomic-embed-text"}),
        encoding="utf-8",
    )
    config = _import_embedding_config(tmp_path, monkeypatch)

    loaded = config.load_embedding_endpoint_config()

    assert loaded == {"url": "http://localhost:11434/v1/embeddings", "model": "nomic-embed-text"}
    assert _stored_embedding_value() == loaded


def test_embedding_endpoint_save_and_clear_use_database(tmp_path, monkeypatch):
    config = _import_embedding_config(tmp_path, monkeypatch)

    config.save_embedding_endpoint_config({
        "url": "http://localhost:8000/v1/embeddings",
        "model": "embed-model",
    })

    assert not (tmp_path / "embedding_endpoint.json").exists()
    assert config.load_embedding_endpoint_config()["model"] == "embed-model"
    assert _stored_embedding_value()["url"] == "http://localhost:8000/v1/embeddings"

    config.clear_embedding_endpoint_config()

    assert config.load_embedding_endpoint_config() == {}
    assert _stored_embedding_value() is None
