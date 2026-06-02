import importlib
import json
import sys
from types import SimpleNamespace


def _fresh_modules(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    for name in (
        "src.memory_tidy_store",
        "services.memory.memory_extractor",
        "routes.admin_wipe_routes",
        "core.database",
    ):
        sys.modules.pop(name, None)
    if "core" in sys.modules and hasattr(sys.modules["core"], "database"):
        delattr(sys.modules["core"], "database")

    from core.database import Base, engine

    Base.metadata.create_all(bind=engine)
    extractor = importlib.import_module("services.memory.memory_extractor")
    store = importlib.import_module("src.memory_tidy_store")
    return extractor, store


def _memory_manager(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return SimpleNamespace(memory_file=str(data_dir / "memory.json"))


def _stored_tidy_state():
    from core.database import AppSetting, SessionLocal

    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == "memory_tidy_state").one_or_none()
        return dict(row.value or {}) if row is not None else None
    finally:
        db.close()


def test_memory_tidy_state_imports_legacy_sidecar(tmp_path, monkeypatch):
    extractor, _store = _fresh_modules(tmp_path, monkeypatch)
    manager = _memory_manager(tmp_path)
    sidecar = tmp_path / "data" / "memory_tidy_state.json"
    sidecar.write_text(json.dumps({"alice": {"fingerprint": "abc"}}), encoding="utf-8")

    loaded = extractor._load_tidy_state(manager)

    assert loaded == {"alice": {"fingerprint": "abc"}}
    assert _stored_tidy_state() == loaded


def test_memory_tidy_state_save_uses_database(tmp_path, monkeypatch):
    extractor, _store = _fresh_modules(tmp_path, monkeypatch)
    manager = _memory_manager(tmp_path)

    extractor._save_tidy_state(manager, "alice", "abc")

    assert not (tmp_path / "data" / "memory_tidy_state.json").exists()
    assert extractor._load_tidy_state(manager) == {"alice": {"fingerprint": "abc"}}
    assert _stored_tidy_state() == {"alice": {"fingerprint": "abc"}}


def test_admin_memory_wipe_clears_db_tidy_state(tmp_path, monkeypatch):
    _extractor, store = _fresh_modules(tmp_path, monkeypatch)
    manager = _memory_manager(tmp_path)
    store.save_memory_tidy_state(tmp_path / "data" / "memory_tidy_state.json", {"alice": {"fingerprint": "abc"}})
    (tmp_path / "data" / "memory.json").write_text("[]", encoding="utf-8")

    admin = importlib.import_module("routes.admin_wipe_routes")
    monkeypatch.setattr(admin, "DATA_DIR", str(tmp_path / "data"))
    admin._wipe_memory_files()

    assert _stored_tidy_state() is None
    assert json.loads((tmp_path / "data" / "memory.json").read_text(encoding="utf-8")) == []
