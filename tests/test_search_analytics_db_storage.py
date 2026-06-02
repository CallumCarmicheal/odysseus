import importlib
import json
import sys


def _fresh_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for name in (
        "src.search_analytics_store",
        "src.search.analytics",
        "services.search.analytics",
        "core.database",
    ):
        sys.modules.pop(name, None)
    if "core" in sys.modules and hasattr(sys.modules["core"], "database"):
        delattr(sys.modules["core"], "database")

    store = importlib.import_module("src.search_analytics_store")
    from core.database import Base, engine

    Base.metadata.create_all(bind=engine)
    return store


def _stored_analytics():
    from core.database import AppSetting, SessionLocal

    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == "search_analytics").one_or_none()
        return dict(row.value or {}) if row is not None else None
    finally:
        db.close()


def test_search_analytics_imports_legacy_json_to_database(tmp_path, monkeypatch):
    store = _fresh_store(tmp_path, monkeypatch)
    legacy = tmp_path / "search_analytics.json"
    legacy.write_text(json.dumps({
        "total_queries": 2,
        "successful_queries": 1,
        "failed_queries": 1,
        "cache_hits": 1,
        "cache_misses": 1,
        "query_patterns": {"weather": {"count": 2, "successes": 1}},
    }), encoding="utf-8")

    loaded = store.load_search_analytics(legacy_paths=[legacy])

    assert loaded["total_queries"] == 2
    assert loaded["query_patterns"]["weather"]["successes"] == 1
    assert _stored_analytics()["cache_hits"] == 1


def test_search_analytics_record_query_uses_database(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    analytics = importlib.import_module("src.search.analytics")

    analytics._record_query("weather", success=True, cache_hit=False)

    assert not (tmp_path / "src" / "search_analytics.json").exists()
    stored = _stored_analytics()
    assert stored["total_queries"] == 1
    assert stored["successful_queries"] == 1
    assert stored["cache_misses"] == 1
    assert stored["query_patterns"]["weather"] == {"count": 1, "successes": 1}


def test_services_search_analytics_reads_same_database_state(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    src_analytics = importlib.import_module("src.search.analytics")
    svc_analytics = importlib.import_module("services.search.analytics")

    src_analytics._record_query("sqlite", success=True, cache_hit=True)
    stats = svc_analytics.get_search_stats()

    assert stats["total_queries"] == 1
    assert stats["most_common_queries"] == ["sqlite"]
    assert stats["cache_hit_rate"] == 1
