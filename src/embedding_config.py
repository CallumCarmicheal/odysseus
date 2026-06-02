"""Embedding endpoint configuration storage.

Runtime endpoint config is stored in app.db. Legacy
data/embedding_endpoint.json is imported and kept as the DB-unavailable
fallback.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

from core.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

_SETTING_KEY = "embedding_endpoint"
ENDPOINT_FILE = Path(os.environ.get(
    "ODYSSEUS_EMBEDDING_ENDPOINT_FILE",
    Path(__file__).resolve().parent.parent / "data" / "embedding_endpoint.json",
))


def _load_legacy_endpoint() -> Dict[str, Any]:
    try:
        if ENDPOINT_FILE.exists():
            data = json.loads(ENDPOINT_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _save_legacy_endpoint(data: Dict[str, Any]) -> None:
    ENDPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(ENDPOINT_FILE), dict(data or {}), indent=2)


def _delete_legacy_endpoint() -> None:
    try:
        ENDPOINT_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _session():
    from core.database import AppSetting, Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine, tables=[AppSetting.__table__])
    return SessionLocal()


def _ensure_legacy_imported(db) -> None:
    from core.database import AppSetting

    legacy = _load_legacy_endpoint()
    if not legacy.get("url"):
        return
    existing = db.query(AppSetting).filter(AppSetting.key == _SETTING_KEY).first()
    if existing is None:
        db.add(AppSetting(key=_SETTING_KEY, value=dict(legacy)))
        logger.info("Imported legacy embedding endpoint config into app.db")


def _load_db() -> Dict[str, Any]:
    from core.database import AppSetting

    db = _session()
    try:
        _ensure_legacy_imported(db)
        changed = bool(db.new or db.dirty)
        if changed:
            db.flush()
        row = db.query(AppSetting).filter(AppSetting.key == _SETTING_KEY).first()
        value = dict(row.value or {}) if row is not None else {}
        if changed or db.new or db.dirty:
            db.commit()
        return value
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _save_db(data: Dict[str, Any]) -> None:
    from core.database import AppSetting

    db = _session()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == _SETTING_KEY).first()
        if row is None:
            db.add(AppSetting(key=_SETTING_KEY, value=dict(data or {})))
        else:
            row.value = dict(data or {})
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _clear_db() -> None:
    from core.database import AppSetting

    db = _session()
    try:
        db.query(AppSetting).filter(AppSetting.key == _SETTING_KEY).delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def load_embedding_endpoint_config() -> Dict[str, Any]:
    try:
        return _load_db()
    except Exception as exc:
        logger.warning("DB-backed embedding endpoint unavailable; falling back to JSON: %s", exc)
        return _load_legacy_endpoint()


def save_embedding_endpoint_config(data: Dict[str, Any]) -> None:
    try:
        _save_db(data)
        return
    except Exception as exc:
        logger.warning("DB-backed embedding endpoint unavailable; falling back to JSON: %s", exc)
    _save_legacy_endpoint(data)


def clear_embedding_endpoint_config() -> None:
    try:
        _clear_db()
    except Exception as exc:
        logger.warning("DB-backed embedding endpoint unavailable while clearing: %s", exc)
    _delete_legacy_endpoint()
