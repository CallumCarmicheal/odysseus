import importlib
import sqlite3
import sys
from pathlib import Path

import pytest


def _drop_modules():
    for name in ("routes.email_helpers", "core.database"):
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _fresh_modules():
    _drop_modules()
    yield
    _drop_modules()


def _import_helpers(monkeypatch, app_db: Path, legacy_db: Path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{app_db}")
    monkeypatch.setenv("ODYSSEUS_LEGACY_SCHEDULED_EMAILS_DB", str(legacy_db))
    return importlib.import_module("routes.email_helpers")


def test_scheduled_email_sidecar_imports_into_app_db(tmp_path, monkeypatch):
    app_db = tmp_path / "app.db"
    legacy_db = tmp_path / "scheduled_emails.db"

    with sqlite3.connect(legacy_db) as conn:
        conn.execute("""
            CREATE TABLE scheduled_emails (
                id TEXT PRIMARY KEY,
                to_addr TEXT NOT NULL,
                cc TEXT,
                bcc TEXT,
                subject TEXT,
                body TEXT NOT NULL,
                in_reply_to TEXT,
                references_hdr TEXT,
                attachments TEXT,
                send_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT
            )
        """)
        conn.execute("""
            INSERT INTO scheduled_emails
            (id, to_addr, cc, bcc, subject, body, in_reply_to, references_hdr,
             attachments, send_at, created_at, status, error)
            VALUES
            ('sched1', 'to@example.com', NULL, NULL, 'Subject', 'Body', NULL,
             NULL, '[]', '2026-06-02T10:00:00', '2026-06-01T10:00:00',
             'pending', NULL)
        """)
        conn.execute("""
            CREATE TABLE email_ai_replies (
                message_id TEXT PRIMARY KEY,
                uid TEXT,
                folder TEXT,
                reply TEXT NOT NULL,
                model_used TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO email_ai_replies
            (message_id, uid, folder, reply, model_used, created_at)
            VALUES ('<m1>', '42', 'INBOX', 'Draft reply', 'model-a', '2026-06-01T10:00:00')
        """)
        conn.execute("""
            CREATE TABLE email_tags (
                message_id TEXT PRIMARY KEY,
                uid TEXT,
                folder TEXT,
                subject TEXT,
                sender TEXT,
                tags TEXT,
                spam_verdict INTEGER DEFAULT 0,
                spam_reason TEXT,
                moved_to TEXT,
                model_used TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO email_tags
            (message_id, uid, folder, subject, sender, tags, spam_verdict,
             spam_reason, moved_to, model_used, created_at)
            VALUES ('<m1>', '42', 'INBOX', 'Subject', 'sender@example.com',
                    '["urgent"]', 0, '', NULL, 'model-a', '2026-06-01T10:00:00')
        """)

    helpers = _import_helpers(monkeypatch, app_db, legacy_db)

    assert Path(helpers.SCHEDULED_DB).resolve() == app_db.resolve()
    assert Path(helpers.LEGACY_SCHEDULED_DB).resolve() == legacy_db.resolve()

    with sqlite3.connect(app_db) as conn:
        scheduled_cols = [row[1] for row in conn.execute("PRAGMA table_info(scheduled_emails)")]
        assert "account_id" in scheduled_cols
        assert "odysseus_kind" in scheduled_cols
        row = conn.execute(
            "SELECT id, to_addr, account_id, odysseus_kind FROM scheduled_emails WHERE id='sched1'"
        ).fetchone()
        assert row == ("sched1", "to@example.com", None, None)

        reply = conn.execute(
            "SELECT reply, model_used FROM email_ai_replies WHERE message_id='<m1>'"
        ).fetchone()
        assert reply == ("Draft reply", "model-a")

        tag = conn.execute(
            "SELECT owner, tags FROM email_tags WHERE message_id='<m1>'"
        ).fetchone()
        assert tag == ("", '["urgent"]')

    # Import is idempotent and leaves the legacy DB in place for rollback.
    helpers._init_scheduled_db()
    with sqlite3.connect(app_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduled_emails").fetchone()[0] == 1
    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduled_emails").fetchone()[0] == 1


def test_in_memory_database_url_uses_legacy_sqlite_file(tmp_path, monkeypatch):
    legacy_db = tmp_path / "scheduled_emails.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("ODYSSEUS_LEGACY_SCHEDULED_EMAILS_DB", str(legacy_db))

    helpers = importlib.import_module("routes.email_helpers")

    assert Path(helpers.SCHEDULED_DB).resolve() == legacy_db.resolve()
    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scheduled_emails'"
        ).fetchone()
