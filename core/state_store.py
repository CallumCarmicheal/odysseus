"""Database-backed state helpers for settings, features, and preferences.

Legacy JSON files are imported when the matching database table is empty.
The JSON files are not deleted or rewritten here; they remain deployment
fallbacks until the storage migration is complete.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from core.identity import normalize_username, resolve_user_id

logger = logging.getLogger(__name__)

GLOBAL_OWNER_KEY = "__global__"
PREFS_FILE = Path("data/user_prefs.json")
SETTINGS_FILE = Path("data/settings.json")
FEATURES_FILE = Path("data/features.json")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read legacy state file %s: %s", path, exc)
    return {}


def _session():
    from core.database import SessionLocal

    return SessionLocal()


def _flush_pending(db) -> bool:
    changed = bool(db.new or db.dirty or db.deleted)
    if changed:
        db.flush()
    return changed


def _primary_username(db) -> str | None:
    from core.database import User

    row = db.query(User).filter(User.is_admin == True).order_by(User.id).first()  # noqa: E712
    if row is None:
        row = db.query(User).order_by(User.id).first()
    return row.username if row is not None else None


def _owner_parts(db, username: str | None) -> tuple[int | None, str]:
    from core.database import User

    normalized = normalize_username(username)
    if not normalized:
        return None, GLOBAL_OWNER_KEY
    user_id = resolve_user_id(db, User, normalized)
    return user_id, normalized


def _find_preferences_row(db, username: str | None):
    from core.database import UserPreference

    user_id, owner_key = _owner_parts(db, username)
    if user_id is not None:
        row = db.query(UserPreference).filter(UserPreference.user_id == user_id).first()
        if row is not None:
            if row.owner_key != owner_key:
                row.owner_key = owner_key
            return row
    row = db.query(UserPreference).filter(UserPreference.owner_key == owner_key).first()
    if row is not None and user_id is not None and row.user_id != user_id:
        row.user_id = user_id
    return row


def _upsert_preferences(db, username: str | None, preferences: dict[str, Any]) -> None:
    from core.database import UserPreference

    user_id, owner_key = _owner_parts(db, username)
    row = _find_preferences_row(db, username)
    now = datetime.utcnow()
    if row is None:
        db.add(UserPreference(
            user_id=user_id,
            owner_key=owner_key,
            preferences=dict(preferences or {}),
            created_at=now,
            updated_at=now,
        ))
        return
    row.user_id = user_id
    row.owner_key = owner_key
    row.preferences = dict(preferences or {})
    row.updated_at = now


def _ensure_legacy_preferences_imported(db) -> None:
    from core.database import UserPreference

    if db.query(UserPreference).count() > 0:
        return

    raw = _read_json(PREFS_FILE)
    if not raw:
        return

    users = raw.get("_users")
    if isinstance(users, dict):
        imported = 0
        for username, preferences in users.items():
            if isinstance(preferences, dict):
                _upsert_preferences(db, username, preferences)
                imported += 1
        if imported:
            logger.info("Imported %d legacy user preference row(s) into app.db", imported)
        return

    # Flat format -> import under the primary admin user for backward compat.
    primary = _primary_username(db)
    _upsert_preferences(db, primary, raw)
    logger.info(
        "Imported legacy flat user preferences into app.db under '%s'",
        primary or GLOBAL_OWNER_KEY,
    )


def load_user_preferences(username: str | None = None) -> dict[str, Any]:
    """Return preferences for a user, falling back to the first row for auth-off."""

    from core.database import UserPreference

    db = _session()
    try:
        _ensure_legacy_preferences_imported(db)
        changed = _flush_pending(db)
        row = _find_preferences_row(db, username)
        if row is None and not normalize_username(username):
            row = db.query(UserPreference).order_by(UserPreference.id).first()
        preferences = dict((row.preferences if row is not None else {}) or {})
        if changed or db.new or db.dirty:
            db.commit()
        return preferences
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def save_user_preferences(username: str | None, preferences: dict[str, Any]) -> None:
    db = _session()
    try:
        _ensure_legacy_preferences_imported(db)
        _flush_pending(db)
        _upsert_preferences(db, username, preferences)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _ensure_legacy_settings_imported(db) -> None:
    from core.database import AppSetting

    if db.query(AppSetting).count() > 0:
        return
    raw = _read_json(SETTINGS_FILE)
    if not raw:
        return
    now = datetime.utcnow()
    for key, value in raw.items():
        db.add(AppSetting(key=str(key), value=value, created_at=now, updated_at=now))
    logger.info("Imported %d legacy app setting row(s) into app.db", len(raw))


def load_app_settings() -> dict[str, Any]:
    from core.database import AppSetting

    db = _session()
    try:
        _ensure_legacy_settings_imported(db)
        changed = _flush_pending(db)
        rows = db.query(AppSetting).all()
        values = {row.key: row.value for row in rows}
        if changed or db.new or db.dirty:
            db.commit()
        return values
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def save_app_settings(settings: dict[str, Any]) -> None:
    from core.database import AppSetting

    db = _session()
    try:
        _ensure_legacy_settings_imported(db)
        _flush_pending(db)
        desired_keys = {str(key) for key in (settings or {})}
        for row in db.query(AppSetting).all():
            if row.key not in desired_keys:
                db.delete(row)
        now = datetime.utcnow()
        for key, value in (settings or {}).items():
            row = db.query(AppSetting).filter(AppSetting.key == str(key)).first()
            if row is None:
                db.add(AppSetting(key=str(key), value=value, created_at=now, updated_at=now))
            else:
                row.value = value
                row.updated_at = now
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _ensure_legacy_features_imported(db) -> None:
    from core.database import FeatureFlag

    if db.query(FeatureFlag).count() > 0:
        return
    raw = _read_json(FEATURES_FILE)
    if not raw:
        return
    now = datetime.utcnow()
    for key, value in raw.items():
        db.add(FeatureFlag(key=str(key), enabled=bool(value), created_at=now, updated_at=now))
    logger.info("Imported %d legacy feature flag row(s) into app.db", len(raw))


def load_feature_flags() -> dict[str, bool]:
    from core.database import FeatureFlag

    db = _session()
    try:
        _ensure_legacy_features_imported(db)
        changed = _flush_pending(db)
        rows = db.query(FeatureFlag).all()
        values = {row.key: bool(row.enabled) for row in rows}
        if changed or db.new or db.dirty:
            db.commit()
        return values
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def save_feature_flags(features: dict[str, Any]) -> None:
    from core.database import FeatureFlag

    db = _session()
    try:
        _ensure_legacy_features_imported(db)
        _flush_pending(db)
        desired_keys = {str(key) for key in (features or {})}
        for row in db.query(FeatureFlag).all():
            if row.key not in desired_keys:
                db.delete(row)
        now = datetime.utcnow()
        for key, value in (features or {}).items():
            row = db.query(FeatureFlag).filter(FeatureFlag.key == str(key)).first()
            if row is None:
                db.add(FeatureFlag(key=str(key), enabled=bool(value), created_at=now, updated_at=now))
            else:
                row.enabled = bool(value)
                row.updated_at = now
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
