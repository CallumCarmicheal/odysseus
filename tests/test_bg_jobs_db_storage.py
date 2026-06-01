import importlib
import json
import sqlite3
import sys


def _import_bg_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    sys.modules.pop("core.database", None)
    sys.modules.pop("src.bg_jobs", None)
    return importlib.import_module("src.bg_jobs")


def test_bg_jobs_imports_legacy_json_into_app_db(tmp_path, monkeypatch):
    legacy = {
        "job1": {
            "id": "job1",
            "session_id": "sess1",
            "command": "echo ok",
            "status": "done",
            "followed_up": False,
        }
    }
    (tmp_path / "bg_jobs.json").write_text(json.dumps(legacy), encoding="utf-8")

    bg_jobs = _import_bg_jobs(tmp_path, monkeypatch)

    assert bg_jobs._load()["job1"]["command"] == "echo ok"
    with sqlite3.connect(tmp_path / "app.db") as conn:
        row = conn.execute("SELECT record FROM background_jobs WHERE id='job1'").fetchone()
        assert json.loads(row[0])["session_id"] == "sess1"


def test_bg_jobs_save_load_uses_app_db(tmp_path, monkeypatch):
    bg_jobs = _import_bg_jobs(tmp_path, monkeypatch)

    bg_jobs._save({
        "job2": {
            "id": "job2",
            "session_id": "sess2",
            "command": "echo later",
            "status": "running",
            "followed_up": False,
        }
    })

    assert not (tmp_path / "bg_jobs.json").exists()
    loaded = bg_jobs._load()
    assert loaded["job2"]["status"] == "running"
    with sqlite3.connect(tmp_path / "app.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0] == 1
