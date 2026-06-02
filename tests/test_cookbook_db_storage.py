import asyncio
import importlib
import json
import sys
import time
from types import SimpleNamespace


def _fresh_cookbook_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for name in ("src.cookbook_store", "core.database", "routes.cookbook_routes"):
        sys.modules.pop(name, None)
    if "core" in sys.modules and hasattr(sys.modules["core"], "database"):
        delattr(sys.modules["core"], "database")

    store = importlib.import_module("src.cookbook_store")
    from core.database import Base, engine

    Base.metadata.create_all(bind=engine)
    return store


def _stored_cookbook_state():
    from core.database import AppSetting, SessionLocal

    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == "cookbook_state").one_or_none()
        return dict(row.value or {}) if row is not None else None
    finally:
        db.close()


class _JsonRequest:
    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.state = SimpleNamespace(current_user="internal-tool")

    async def json(self):
        return self._body


def test_cookbook_state_imports_legacy_json_to_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    legacy = {
        "env": {"servers": [{"host": "gpu-box"}]},
        "presets": [{"name": "serve-a"}],
        "tasks": [],
    }
    (data_dir / "cookbook_state.json").write_text(json.dumps(legacy), encoding="utf-8")
    store = _fresh_cookbook_store(tmp_path, monkeypatch)

    loaded = store.load_cookbook_state()

    assert loaded == legacy
    assert _stored_cookbook_state() == legacy


def test_cookbook_state_save_uses_database(tmp_path, monkeypatch):
    store = _fresh_cookbook_store(tmp_path, monkeypatch)
    state = {"tasks": [{"sessionId": "cookbook-a"}], "env": {"servers": []}}

    store.save_cookbook_state(state)

    assert not (tmp_path / "data" / "cookbook_state.json").exists()
    assert store.load_cookbook_state() == state
    assert _stored_cookbook_state() == state


def test_cookbook_state_route_preserves_recent_server_tasks(tmp_path, monkeypatch):
    store = _fresh_cookbook_store(tmp_path, monkeypatch)
    now_ms = int(time.time() * 1000)
    store.save_cookbook_state({
        "env": {"servers": [{"host": "gpu-box"}]},
        "tasks": [{"sessionId": "server-task", "ts": now_ms, "type": "download"}],
    })

    from routes.cookbook_routes import setup_cookbook_routes

    router = setup_cookbook_routes()
    endpoint = next(
        r.endpoint
        for r in router.routes
        if getattr(r, "path", "") == "/api/cookbook/state" and "POST" in getattr(r, "methods", set())
    )

    result = asyncio.run(endpoint(_JsonRequest({"env": {"servers": []}, "tasks": []})))
    saved = store.load_cookbook_state()

    assert result["ok"] is True
    assert result["preserved"] == 1
    assert saved["env"]["servers"] == [{"host": "gpu-box"}]
    assert saved["tasks"][0]["sessionId"] == "server-task"
