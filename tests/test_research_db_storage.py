import asyncio
import importlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock


def _fresh_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    for name in ("src.research_store", "core.database", "routes.research_routes"):
        sys.modules.pop(name, None)
    if "core" in sys.modules and hasattr(sys.modules["core"], "database"):
        delattr(sys.modules["core"], "database")

    store = importlib.import_module("src.research_store")
    from core.database import Base, User, engine

    Base.metadata.create_all(bind=engine)
    db = importlib.import_module("core.database").SessionLocal()
    try:
        db.add_all([
            User(public_id="user_alice", username="alice", password_hash="x", is_admin=False, privileges={}),
            User(public_id="user_bob", username="bob", password_hash="x", is_admin=False, privileges={}),
        ])
        db.commit()
    finally:
        db.close()
    return store


def _stored_research(public_id):
    from core.database import ResearchResult, SessionLocal

    db = SessionLocal()
    try:
        return db.query(ResearchResult).filter(ResearchResult.public_id == public_id).one_or_none()
    finally:
        db.close()


def _fake_request(user):
    req = SimpleNamespace()
    req.state = SimpleNamespace(current_user=user)
    req.client = SimpleNamespace(host="127.0.0.1")
    return req


def test_research_save_uses_database_and_owner_id(tmp_path, monkeypatch):
    store = _fresh_store(tmp_path, monkeypatch)

    store.save_research_result("rp-db", {
        "query": "sqlite storage",
        "status": "done",
        "result": "report",
        "sources": [{"url": "https://example.com", "title": "Example"}],
        "raw_findings": [{"url": "https://example.com", "title": "Example", "summary": "ok"}],
        "stats": {"Rounds": 2},
        "started_at": 10,
        "completed_at": 25,
        "owner": "alice",
    })

    assert not (tmp_path / "data" / "deep_research" / "rp-db.json").exists()
    loaded = store.load_research_result("rp-db")
    assert loaded["query"] == "sqlite storage"
    assert loaded["sources"][0]["url"] == "https://example.com"

    row = _stored_research("rp-db")
    assert row is not None
    assert row.owner == "alice"
    assert row.owner_id is not None


def test_research_load_imports_legacy_json(tmp_path, monkeypatch):
    store = _fresh_store(tmp_path, monkeypatch)
    legacy_dir = tmp_path / "data" / "deep_research"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "legacy-id.json").write_text(json.dumps({
        "query": "legacy import",
        "status": "done",
        "result": "legacy report",
        "sources": [],
        "started_at": 5,
        "completed_at": 9,
        "owner": "bob",
    }), encoding="utf-8")

    loaded = store.load_research_result("legacy-id")

    assert loaded["result"] == "legacy report"
    row = _stored_research("legacy-id")
    assert row is not None
    assert row.owner == "bob"


def test_research_library_route_uses_db_and_filters_owner(tmp_path, monkeypatch):
    store = _fresh_store(tmp_path, monkeypatch)
    store.save_research_result("alice-id", {
        "query": "alice query",
        "status": "done",
        "result": "alice report",
        "sources": [{"url": "https://a.test"}],
        "started_at": 1,
        "completed_at": 2,
        "owner": "alice",
    })
    store.save_research_result("bob-id", {
        "query": "bob query",
        "status": "done",
        "result": "bob report",
        "sources": [{"url": "https://b.test"}],
        "started_at": 3,
        "completed_at": 4,
        "owner": "bob",
    })

    from routes.research_routes import setup_research_routes

    rh = MagicMock()
    rh._active_tasks = {}
    router = setup_research_routes(rh)
    endpoint = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/research/library")

    result = asyncio.run(endpoint(request=_fake_request("alice"), search=None, sort="recent", limit=50, archived=False))

    assert result["total"] == 1
    assert result["research"][0]["id"] == "alice-id"
    assert result["research"][0]["source_count"] == 1
