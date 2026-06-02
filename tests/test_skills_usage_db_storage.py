import importlib
import json
import sys
from types import SimpleNamespace


def _fresh_modules(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    for name in (
        "src.skills_usage_store",
        "services.memory.skills",
        "routes.admin_wipe_routes",
        "core.database",
    ):
        sys.modules.pop(name, None)
    if "core" in sys.modules and hasattr(sys.modules["core"], "database"):
        delattr(sys.modules["core"], "database")

    from core.database import Base, engine

    Base.metadata.create_all(bind=engine)
    skills_mod = importlib.import_module("services.memory.skills")
    store = importlib.import_module("src.skills_usage_store")
    return skills_mod, store


def _stored_usage():
    from core.database import AppSetting, SessionLocal

    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == "skills_usage").one_or_none()
        return dict(row.value or {}) if row is not None else None
    finally:
        db.close()


def test_skills_usage_imports_legacy_sidecar(tmp_path, monkeypatch):
    skills_mod, _store = _fresh_modules(tmp_path, monkeypatch)
    usage_dir = tmp_path / "data" / "skills"
    usage_dir.mkdir(parents=True)
    (usage_dir / "_usage.json").write_text(json.dumps({
        "debug-python": {"uses": 2, "last_used": 123},
    }), encoding="utf-8")

    manager = skills_mod.SkillsManager(str(tmp_path / "data"))
    loaded = manager._load_usage()

    assert loaded == {"debug-python": {"uses": 2, "last_used": 123}}
    assert _stored_usage() == loaded


def test_record_use_saves_skills_usage_in_database(tmp_path, monkeypatch):
    skills_mod, _store = _fresh_modules(tmp_path, monkeypatch)
    manager = skills_mod.SkillsManager(str(tmp_path / "data"))

    manager.record_use("debug-python")

    assert not (tmp_path / "data" / "skills" / "_usage.json").exists()
    stored = _stored_usage()
    assert stored["debug-python"]["uses"] == 1
    assert isinstance(stored["debug-python"]["last_used"], int)


def test_admin_skills_wipe_clears_db_usage(tmp_path, monkeypatch):
    _skills_mod, store = _fresh_modules(tmp_path, monkeypatch)
    data_dir = tmp_path / "data"
    skills_dir = data_dir / "skills" / "general" / "debug-python"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("---\nname: debug-python\n---\n", encoding="utf-8")
    store.save_skills_usage(data_dir / "skills" / "_usage.json", {"debug-python": {"uses": 3}})

    admin = importlib.import_module("routes.admin_wipe_routes")
    monkeypatch.setattr(admin, "DATA_DIR", str(data_dir))
    router = admin.setup_admin_wipe_routes(SimpleNamespace(sessions={}))
    endpoint = next(r.endpoint for r in router.routes if getattr(r, "path", "").endswith("/wipe/{kind}"))
    request = SimpleNamespace(headers={}, state=SimpleNamespace(current_user="internal-tool"))

    result = endpoint("skills", request)

    assert result["status"] == "deleted"
    assert result["count"] == 1
    assert _stored_usage() is None
    assert not (data_dir / "skills").exists()
