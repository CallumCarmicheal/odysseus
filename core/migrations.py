"""Deployment-safe migration helpers for Odysseus.

The current project uses hand-written startup migrations in ``core.database``.
This module adds the missing ledger and reusable helpers so future migrations
can be ordered, idempotent, auditable, and safer for existing deployments.
"""

from __future__ import annotations

import hashlib
import inspect
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from sqlalchemy import text


SCHEMA_MIGRATIONS_TABLE = "schema_migrations"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied safely."""


@dataclass(frozen=True)
class Migration:
    """One forward-only database migration."""

    version: str
    name: str
    apply: Callable[[Any], None]
    checksum: str | None = None
    backup_before: bool = False


@dataclass
class MigrationRunResult:
    """Summary of one migration runner invocation."""

    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    backup_path: Path | None = None


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _validate_identifier(identifier: str) -> str:
    if not _IDENT_RE.fullmatch(identifier or ""):
        raise ValueError(f"Invalid SQL identifier: {identifier!r}")
    return identifier


def migration_checksum(migration: Migration) -> str:
    """Return the stable checksum for a migration definition."""

    if migration.checksum:
        return migration.checksum
    try:
        source = inspect.getsource(migration.apply)
    except (OSError, TypeError):
        source = repr(migration.apply)
    payload = f"{migration.version}\n{migration.name}\n{source}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def ensure_schema_migrations(engine: Any) -> None:
    """Create the migration ledger if it does not exist."""

    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (
                version TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 1,
                error TEXT
            )
        """))
        conn.execute(text(f"""
            CREATE INDEX IF NOT EXISTS ix_{SCHEMA_MIGRATIONS_TABLE}_success
            ON {SCHEMA_MIGRATIONS_TABLE}(success, applied_at)
        """))


def get_applied_migrations(engine: Any) -> dict[str, dict[str, Any]]:
    """Return migration ledger rows keyed by version."""

    ensure_schema_migrations(engine)
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT version, name, checksum, applied_at, duration_ms, success, error
            FROM {SCHEMA_MIGRATIONS_TABLE}
        """)).mappings().all()
    return {row["version"]: dict(row) for row in rows}


def _record_migration(
    engine: Any,
    migration: Migration,
    checksum: str,
    duration_ms: int,
    success: bool,
    error: str | None = None,
) -> None:
    with engine.begin() as conn:
        exists = conn.execute(
            text(f"SELECT 1 FROM {SCHEMA_MIGRATIONS_TABLE} WHERE version = :version"),
            {"version": migration.version},
        ).first()
        values = {
            "version": migration.version,
            "name": migration.name,
            "checksum": checksum,
            "applied_at": _utcnow_iso(),
            "duration_ms": duration_ms,
            "success": 1 if success else 0,
            "error": (error or "")[:4000] if error else None,
        }
        if exists:
            conn.execute(text(f"""
                UPDATE {SCHEMA_MIGRATIONS_TABLE}
                   SET name = :name,
                       checksum = :checksum,
                       applied_at = :applied_at,
                       duration_ms = :duration_ms,
                       success = :success,
                       error = :error
                 WHERE version = :version
            """), values)
        else:
            conn.execute(text(f"""
                INSERT INTO {SCHEMA_MIGRATIONS_TABLE}
                    (version, name, checksum, applied_at, duration_ms, success, error)
                VALUES
                    (:version, :name, :checksum, :applied_at, :duration_ms, :success, :error)
            """), values)


def sqlite_path_from_url(database_url: str) -> Path | None:
    """Return the filesystem path for a SQLite URL, or ``None`` for memory DBs."""

    prefix = "sqlite:///"
    if not (database_url or "").startswith(prefix):
        return None
    raw = database_url[len(prefix):]
    if raw in ("", ":memory:"):
        return None
    return Path(raw).resolve()


def create_sqlite_backup(
    database_url: str,
    label: str = "pre-migration",
    backup_dir: str | Path | None = None,
) -> Path | None:
    """Copy a SQLite database before a risky migration.

    Returns the backup path, or ``None`` when the database is in-memory or the
    source file does not exist yet.
    """

    source = sqlite_path_from_url(database_url)
    if source is None or not source.exists():
        return None
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "migration"
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    target_dir = Path(backup_dir).resolve() if backup_dir else source.parent / "migration_backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{source.stem}.{timestamp}.{safe_label}{source.suffix}"
    shutil.copy2(source, target)
    return target


def run_migrations(
    engine: Any,
    migrations: Iterable[Migration],
    *,
    database_url: str | None = None,
) -> MigrationRunResult:
    """Apply pending migrations in version order."""

    ensure_schema_migrations(engine)
    result = MigrationRunResult()
    applied = get_applied_migrations(engine)
    backup_taken = False

    for migration in sorted(migrations, key=lambda item: item.version):
        checksum = migration_checksum(migration)
        existing = applied.get(migration.version)
        if existing and existing.get("success") and existing.get("checksum") == checksum:
            result.skipped.append(migration.version)
            continue
        if existing and existing.get("success") and existing.get("checksum") != checksum:
            raise MigrationError(
                f"Migration {migration.version} was already applied with a different checksum"
            )

        if migration.backup_before and database_url and not backup_taken:
            result.backup_path = create_sqlite_backup(database_url, migration.version)
            backup_taken = True

        started = time.perf_counter()
        try:
            migration.apply(engine)
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            _record_migration(engine, migration, checksum, duration_ms, False, str(exc))
            result.failed.append(migration.version)
            raise MigrationError(f"Migration {migration.version} failed: {exc}") from exc

        duration_ms = int((time.perf_counter() - started) * 1000)
        _record_migration(engine, migration, checksum, duration_ms, True)
        result.applied.append(migration.version)
        applied[migration.version] = {
            "checksum": checksum,
            "success": 1,
        }

    return result


def table_exists(engine: Any, table_name: str) -> bool:
    table_name = _validate_identifier(table_name)
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT 1
              FROM sqlite_master
             WHERE type = 'table' AND name = :name
        """), {"name": table_name}).first()
    return row is not None


def get_table_columns(engine: Any, table_name: str) -> list[str]:
    table_name = _validate_identifier(table_name)
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return [row[1] for row in rows]


def column_exists(engine: Any, table_name: str, column_name: str) -> bool:
    _validate_identifier(column_name)
    return column_name in get_table_columns(engine, table_name)


def index_exists(engine: Any, index_name: str) -> bool:
    index_name = _validate_identifier(index_name)
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT 1
              FROM sqlite_master
             WHERE type = 'index' AND name = :name
        """), {"name": index_name}).first()
    return row is not None


def add_column_if_missing(engine: Any, table_name: str, column_sql: str) -> bool:
    """Add a column using a full SQLite column definition if absent."""

    table_name = _validate_identifier(table_name)
    column_name = _validate_identifier(column_sql.strip().split(None, 1)[0])
    if column_exists(engine, table_name, column_name):
        return False
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))
    return True


def create_index_if_missing(engine: Any, index_name: str, table_name: str, columns_sql: str) -> bool:
    """Create a simple index if it does not exist."""

    index_name = _validate_identifier(index_name)
    table_name = _validate_identifier(table_name)
    if index_exists(engine, index_name):
        return False
    with engine.begin() as conn:
        conn.execute(text(f"CREATE INDEX {index_name} ON {table_name}({columns_sql})"))
    return True
