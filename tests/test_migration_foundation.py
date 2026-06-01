import os
import sqlite3

import pytest
from sqlalchemy import create_engine, text

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from core.identity import is_shared_owner, normalize_username
from core.migrations import (
    Migration,
    MigrationError,
    create_sqlite_backup,
    get_applied_migrations,
    run_migrations,
)
from core.public_ids import (
    ensure_public_id,
    external_id_for,
    generate_public_id,
    public_id_from_legacy,
)


def _engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'app.db'}")


def test_run_migrations_records_and_skips_applied_versions(tmp_path):
    engine = _engine(tmp_path)
    calls = []

    def create_sample_table(db_engine):
        calls.append("ran")
        with db_engine.begin() as conn:
            conn.execute(text("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)"))
            conn.execute(text("INSERT INTO sample (value) VALUES ('ok')"))

    migration = Migration(
        version="202606010001",
        name="create sample table",
        apply=create_sample_table,
        checksum="fixed-checksum",
    )

    first = run_migrations(engine, [migration])
    second = run_migrations(engine, [migration])

    assert first.applied == ["202606010001"]
    assert second.skipped == ["202606010001"]
    assert calls == ["ran"]

    rows = get_applied_migrations(engine)
    assert rows["202606010001"]["name"] == "create sample table"
    assert rows["202606010001"]["checksum"] == "fixed-checksum"
    assert rows["202606010001"]["success"] == 1


def test_run_migrations_rejects_checksum_changes_for_applied_versions(tmp_path):
    engine = _engine(tmp_path)

    def noop(_engine):
        return None

    run_migrations(engine, [Migration("202606010002", "noop", noop, checksum="v1")])

    with pytest.raises(MigrationError, match="different checksum"):
        run_migrations(engine, [Migration("202606010002", "noop", noop, checksum="v2")])


def test_failed_migration_records_error_and_can_retry_same_version(tmp_path):
    engine = _engine(tmp_path)
    attempts = {"count": 0}

    def flaky(_engine):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("boom")
        with _engine.begin() as conn:
            conn.execute(text("CREATE TABLE recovered (id INTEGER PRIMARY KEY)"))

    migration = Migration("202606010003", "flaky", flaky, checksum="same")

    with pytest.raises(MigrationError, match="boom"):
        run_migrations(engine, [migration])

    failed = get_applied_migrations(engine)["202606010003"]
    assert failed["success"] == 0
    assert "boom" in failed["error"]

    retried = run_migrations(engine, [migration])
    assert retried.applied == ["202606010003"]
    assert get_applied_migrations(engine)["202606010003"]["success"] == 1


def test_create_sqlite_backup_copies_existing_database(tmp_path):
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")

    backup = create_sqlite_backup(f"sqlite:///{db_path}", "before-rebuild")

    assert backup is not None
    assert backup.exists()
    assert backup.read_bytes() == db_path.read_bytes()
    assert "before-rebuild" in backup.name


def test_public_id_helpers_preserve_legacy_ids_and_generate_future_ids():
    class Row:
        id = 42
        public_id = ""

    assert public_id_from_legacy(" old-id ") == "old-id"
    generated = generate_public_id("sess")
    assert generated.startswith("sess_")
    assert len(generated) == len("sess_") + 32

    row = Row()
    assert ensure_public_id(row, legacy_id="legacy-session") == "legacy-session"
    assert external_id_for(row) == "legacy-session"


def test_identity_helpers_match_auth_username_semantics():
    assert normalize_username(" AdminUser ") == "adminuser"
    assert is_shared_owner(None)
    assert is_shared_owner("")
    assert not is_shared_owner("alice")
