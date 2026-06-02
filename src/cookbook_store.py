"""DB-backed Cookbook state storage with legacy JSON fallback."""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

COOKBOOK_STATE_KEY = "cookbook_state"
COOKBOOK_STATE_FILE = Path(os.environ.get("DATA_DIR", "data")) / "cookbook_state.json"


def _normalize_state(state: Any) -> dict:
    return copy.deepcopy(state) if isinstance(state, dict) else {}


def _load_legacy_state() -> dict:
    if not COOKBOOK_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(COOKBOOK_STATE_FILE.read_text(encoding="utf-8"))
        return _normalize_state(data)
    except Exception as exc:
        logger.warning("Failed to read legacy cookbook_state.json: %s", exc)
        return {}


def _write_legacy_state(state: dict) -> None:
    from core.atomic_io import atomic_write_json

    COOKBOOK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(COOKBOOK_STATE_FILE), state, indent=2)


def load_cookbook_state(*, import_legacy: bool = True) -> dict:
    """Load Cookbook state from app.db, importing legacy JSON if needed."""
    try:
        from core.database import AppSetting, SessionLocal

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == COOKBOOK_STATE_KEY).first()
            if row is not None:
                return _normalize_state(row.value)

            legacy = _load_legacy_state()
            if legacy and import_legacy:
                db.add(AppSetting(key=COOKBOOK_STATE_KEY, value=_normalize_state(legacy)))
                db.commit()
            return legacy
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as exc:
        logger.warning("DB-backed cookbook state unavailable; falling back to JSON: %s", exc)
        return _load_legacy_state()


def save_cookbook_state(state: dict, *, fallback_to_json: bool = True) -> bool:
    """Save Cookbook state to app.db, falling back to JSON if needed."""
    payload = _normalize_state(state)
    try:
        from core.database import AppSetting, SessionLocal

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == COOKBOOK_STATE_KEY).first()
            if row is None:
                db.add(AppSetting(key=COOKBOOK_STATE_KEY, value=payload))
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
        logger.warning("DB-backed cookbook state unavailable; falling back to JSON: %s", exc)
        _write_legacy_state(payload)
        return False
