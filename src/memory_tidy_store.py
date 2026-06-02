"""DB-backed memory tidy fingerprint state with legacy JSON fallback."""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MEMORY_TIDY_STATE_KEY = "memory_tidy_state"


def _normalize_state(state: Any) -> dict:
    return copy.deepcopy(state) if isinstance(state, dict) else {}


def _load_legacy(path: str | Path) -> dict:
    legacy_path = Path(path)
    if not legacy_path.exists():
        return {}
    try:
        data = json.loads(legacy_path.read_text(encoding="utf-8"))
        return _normalize_state(data)
    except Exception as exc:
        logger.warning("Failed to load memory tidy state sidecar %s: %s", legacy_path, exc)
        return {}


def _write_legacy(path: str | Path, state: dict) -> None:
    from core.atomic_io import atomic_write_json

    legacy_path = Path(path)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(legacy_path), _normalize_state(state), indent=2)


def load_memory_tidy_state(legacy_path: str | Path, *, import_legacy: bool = True) -> dict:
    """Load memory tidy fingerprints from app.db, importing legacy JSON if needed."""
    try:
        from core.database import AppSetting, SessionLocal

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == MEMORY_TIDY_STATE_KEY).first()
            if row is not None:
                return _normalize_state(row.value)

            legacy = _load_legacy(legacy_path)
            if legacy and import_legacy:
                db.add(AppSetting(key=MEMORY_TIDY_STATE_KEY, value=legacy))
                db.commit()
            return legacy
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as exc:
        logger.warning("DB-backed memory tidy state unavailable; falling back to JSON: %s", exc)
        return _load_legacy(legacy_path)


def save_memory_tidy_state(
    legacy_path: str | Path,
    state: dict,
    *,
    fallback_to_json: bool = True,
) -> bool:
    """Save memory tidy fingerprints to app.db, falling back to JSON if needed."""
    payload = _normalize_state(state)
    try:
        from core.database import AppSetting, SessionLocal

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == MEMORY_TIDY_STATE_KEY).first()
            if row is None:
                db.add(AppSetting(key=MEMORY_TIDY_STATE_KEY, value=payload))
            else:
                row.value = payload
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as exc:
        if not fallback_to_json:
            raise
        logger.warning("DB-backed memory tidy state unavailable; falling back to JSON: %s", exc)
        _write_legacy(legacy_path, payload)
        return False


def clear_memory_tidy_state() -> None:
    """Remove all persisted memory tidy fingerprints from app.db."""
    try:
        from core.database import AppSetting, SessionLocal

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == MEMORY_TIDY_STATE_KEY).first()
            if row is not None:
                db.delete(row)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as exc:
        logger.warning("DB-backed memory tidy state clear failed: %s", exc)
