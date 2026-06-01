"""Vaultwarden configuration storage.

Runtime config is stored in app.db. Legacy data/vault.json is imported and
kept as the DB-unavailable fallback.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

from core.platform_compat import safe_chmod
from src.secret_storage import decrypt, encrypt, is_encrypted

logger = logging.getLogger(__name__)

VAULT_FILE = Path(os.environ.get("ODYSSEUS_VAULT_FILE", "data/vault.json"))
_SETTING_KEY = "vault_config"


def _encrypt_config(config: Dict[str, Any]) -> Dict[str, Any]:
    safe = dict(config or {})
    session = safe.get("session")
    if session:
        safe["session"] = encrypt(str(session))
    return safe


def _decrypt_config(config: Dict[str, Any]) -> Dict[str, Any]:
    decoded = dict(config or {})
    session = decoded.get("session")
    if session:
        decoded["session"] = decrypt(str(session))
    return decoded


def _has_plaintext_session(config: Dict[str, Any]) -> bool:
    session = config.get("session")
    return bool(session) and not is_encrypted(str(session))


def _load_legacy_config() -> dict:
    """Load Vaultwarden config from data/vault.json."""
    if VAULT_FILE.exists():
        try:
            raw = json.loads(VAULT_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if _has_plaintext_session(raw):
                    _save_legacy_config(_decrypt_config(raw))
                return _decrypt_config(raw)
        except Exception:
            pass
    return {}


def _save_legacy_config(config: Dict[str, Any]) -> None:
    VAULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    VAULT_FILE.write_text(json.dumps(_encrypt_config(config), indent=2), encoding="utf-8")
    # POSIX: restrict the BW_SESSION store to 0o600. Windows: no-op (profile dir
    # is ACL-restricted already).
    safe_chmod(str(VAULT_FILE), 0o600)


def _session():
    from core.database import AppSetting, Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine, tables=[AppSetting.__table__])
    return SessionLocal()


def _ensure_legacy_imported(db) -> None:
    from core.database import AppSetting

    legacy = _load_legacy_config()
    if not legacy:
        return
    existing = db.query(AppSetting).filter(AppSetting.key == _SETTING_KEY).first()
    if existing is None:
        db.add(AppSetting(key=_SETTING_KEY, value=_encrypt_config(legacy)))
        logger.info("Imported legacy vault config into app.db")


def _load_db() -> Dict[str, Any]:
    from core.database import AppSetting

    db = _session()
    try:
        _ensure_legacy_imported(db)
        changed = bool(db.new or db.dirty)
        if changed:
            db.flush()
        row = db.query(AppSetting).filter(AppSetting.key == _SETTING_KEY).first()
        value = _decrypt_config(row.value if row is not None else {})
        if changed or db.new or db.dirty:
            db.commit()
        return value
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _save_db(config: Dict[str, Any]) -> None:
    from core.database import AppSetting

    db = _session()
    try:
        _ensure_legacy_imported(db)
        row = db.query(AppSetting).filter(AppSetting.key == _SETTING_KEY).first()
        if row is None:
            db.add(AppSetting(key=_SETTING_KEY, value=_encrypt_config(config)))
        else:
            row.value = _encrypt_config(config)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def load_vault_config() -> Dict[str, Any]:
    try:
        return _load_db()
    except Exception as exc:
        logger.warning("DB-backed vault config unavailable; falling back to JSON: %s", exc)
        return _load_legacy_config()


def save_vault_config(config: Dict[str, Any]) -> None:
    try:
        _save_db(config)
        return
    except Exception as exc:
        logger.warning("DB-backed vault config unavailable; falling back to JSON: %s", exc)
    _save_legacy_config(config)
