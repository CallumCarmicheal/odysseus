"""
Authentication module — multi-user password hashing, session tokens, config persistence.

Legacy ``data/auth.json`` and ``data/sessions.json`` are imported on startup
for existing deployments. Runtime auth state is stored in ``app.db``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import bcrypt
import pyotp

from core.identity import normalize_username
from core.public_ids import generate_public_id

logger = logging.getLogger(__name__)


DEFAULT_PRIVILEGES = {
    "can_use_agent": True,
    "can_use_browser": True,
    "can_use_bash": False,
    "can_use_documents": True,
    "can_use_research": True,
    "can_generate_images": True,
    "can_manage_memory": True,
    "max_messages_per_day": 0,
    "allowed_models": [],
}

# Admins get everything
ADMIN_PRIVILEGES = {
    key: (True if isinstance(value, bool) else (0 if isinstance(value, int) else []))
    for key, value in DEFAULT_PRIVILEGES.items()
}

DEFAULT_AUTH_PATH = os.path.join(Path(__file__).parent.parent, "data", "auth.json")
TOKEN_TTL = 60 * 60 * 24 * 7  # 7 days


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _timestamp_to_datetime(value: Any) -> datetime | None:
    try:
        return datetime.utcfromtimestamp(float(value))
    except Exception:
        return None


class AuthManager:
    """Manages multi-user password, privilege, TOTP, and session-token auth."""

    def __init__(self, auth_path: str = DEFAULT_AUTH_PATH, session_factory=None):
        self.auth_path = auth_path
        self._sessions_path = os.path.join(os.path.dirname(auth_path), "sessions.json")
        self._session_factory = session_factory or self._default_session_factory()
        # Guards mutations of persisted user_sessions rows.
        # Validate/create/revoke run concurrently from the FastAPI threadpool.
        self._sessions_lock = threading.RLock()
        # Guards the first-run setup check-and-write so concurrent requests
        # cannot both observe is_configured==False and both create admin accounts.
        self._setup_lock = threading.Lock()
        self._migrate_legacy_files()

    @staticmethod
    def _default_session_factory():
        from core.database import SessionLocal

        return SessionLocal

    def _db(self):
        return self._session_factory()

    # ------------------------------------------------------------------
    # Legacy import
    # ------------------------------------------------------------------

    def _load_legacy_auth(self) -> dict:
        """Load legacy auth config from disk for one-way import."""
        try:
            if os.path.exists(self.auth_path):
                with open(self.auth_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.error("Failed to load legacy auth config: %s", exc)
        return {}

    def _load_legacy_sessions(self) -> dict:
        """Load persisted legacy session tokens from disk; expired ones are pruned on import."""
        try:
            if os.path.exists(self._sessions_path):
                with open(self._sessions_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.error("Failed to load legacy sessions: %s", exc)
        return {}

    def _legacy_users(self, config: dict) -> dict:
        """Migrate old single-user format into the multi-user import shape."""
        if "password_hash" in config and "users" not in config:
            username = normalize_username(config.get("username") or "admin") or "admin"
            return {
                username: {
                    "password_hash": config.get("password_hash", ""),
                    "created": config.get("created", time.time()),
                    "is_admin": True,
                    "privileges": dict(ADMIN_PRIVILEGES),
                }
            }
        users = config.get("users", {})
        return users if isinstance(users, dict) else {}

    def _migrate_legacy_files(self) -> None:
        from core.database import AuthSetting, User, UserSession, backfill_owner_ids

        legacy_auth = self._load_legacy_auth()
        legacy_users = self._legacy_users(legacy_auth)
        legacy_sessions = self._load_legacy_sessions()
        now_ts = time.time()

        db = self._db()
        try:
            imported_users = 0
            for raw_username, raw_user in legacy_users.items():
                username = normalize_username(raw_username)
                if not username or not isinstance(raw_user, dict):
                    continue
                existing = db.query(User).filter(User.username == username).first()
                # Normalize setup.py's old role='admin' marker to is_admin=True.
                is_admin = bool(raw_user.get("is_admin") or raw_user.get("role") == "admin")
                privileges = raw_user.get("privileges")
                if not isinstance(privileges, dict):
                    privileges = dict(ADMIN_PRIVILEGES if is_admin else DEFAULT_PRIVILEGES)
                if existing is None:
                    created_at = _timestamp_to_datetime(raw_user.get("created")) or datetime.utcnow()
                    db.add(User(
                        public_id=generate_public_id("user"),
                        username=username,
                        password_hash=raw_user.get("password_hash") or "",
                        is_admin=is_admin,
                        privileges=privileges,
                        totp_enabled=bool(raw_user.get("totp_enabled")),
                        totp_secret=raw_user.get("totp_secret"),
                        totp_secret_pending=raw_user.get("totp_secret_pending"),
                        totp_backup_codes=raw_user.get("totp_backup_codes") or [],
                        created_at=created_at,
                        updated_at=datetime.utcnow(),
                    ))
                    imported_users += 1
                else:
                    changed = False
                    if is_admin and not existing.is_admin:
                        existing.is_admin = True
                        changed = True
                    if not existing.privileges:
                        existing.privileges = privileges
                        changed = True
                    if changed:
                        existing.updated_at = datetime.utcnow()

            if "signup_enabled" in legacy_auth:
                setting = db.query(AuthSetting).filter(AuthSetting.key == "signup_enabled").first()
                if setting is None:
                    db.add(AuthSetting(key="signup_enabled", value=bool(legacy_auth.get("signup_enabled"))))

            db.commit()
            if imported_users:
                logger.info("Imported %d legacy auth user(s) into app.db", imported_users)
            backfill_owner_ids(db.get_bind())

            imported_sessions = 0
            for token, raw_session in legacy_sessions.items():
                if not token or not isinstance(raw_session, dict):
                    continue
                expiry = float(raw_session.get("expiry") or 0)
                if expiry <= now_ts:
                    continue
                username = normalize_username(raw_session.get("username"))
                if not username:
                    continue
                user = db.query(User).filter(User.username == username).first()
                if user is None:
                    continue
                token_hash = _hash_token(token)
                existing = db.query(UserSession).filter(UserSession.token_hash == token_hash).first()
                if existing is None:
                    db.add(UserSession(
                        token_hash=token_hash,
                        user_id=user.id,
                        expires_at=datetime.utcfromtimestamp(expiry),
                    ))
                    imported_sessions += 1
            db.commit()
            if imported_sessions:
                logger.info("Imported %d legacy login session(s) into app.db", imported_sessions)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------
    # User serialization/settings
    # ------------------------------------------------------------------

    @staticmethod
    def _user_to_dict(user) -> dict:
        return {
            "password_hash": user.password_hash,
            "created": user.created_at.timestamp() if user.created_at else 0,
            "is_admin": bool(user.is_admin),
            "privileges": dict(user.privileges or {}),
            "totp_enabled": bool(user.totp_enabled),
            "totp_secret": user.totp_secret,
            "totp_secret_pending": user.totp_secret_pending,
            "totp_backup_codes": list(user.totp_backup_codes or []),
        }

    def _get_user(self, db, username: str):
        from core.database import User

        username = normalize_username(username)
        if not username:
            return None
        return db.query(User).filter(User.username == username).first()

    @property
    def users(self) -> Dict[str, Any]:
        from core.database import User

        db = self._db()
        try:
            rows = db.query(User).order_by(User.username).all()
            return {row.username: self._user_to_dict(row) for row in rows}
        finally:
            db.close()

    @property
    def signup_enabled(self) -> bool:
        from core.database import AuthSetting

        db = self._db()
        try:
            setting = db.query(AuthSetting).filter(AuthSetting.key == "signup_enabled").first()
            return bool(setting.value) if setting is not None else False
        finally:
            db.close()

    @signup_enabled.setter
    def signup_enabled(self, value: bool):
        from core.database import AuthSetting

        db = self._db()
        try:
            setting = db.query(AuthSetting).filter(AuthSetting.key == "signup_enabled").first()
            if setting is None:
                db.add(AuthSetting(key="signup_enabled", value=bool(value)))
            else:
                setting.value = bool(value)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @property
    def is_configured(self) -> bool:
        from core.database import User

        db = self._db()
        try:
            return (db.query(User).count() or 0) > 0
        finally:
            db.close()

    def primary_admin_username(self) -> Optional[str]:
        from core.database import User

        db = self._db()
        try:
            row = db.query(User).filter(User.is_admin == True).order_by(User.id).first()  # noqa: E712
            if row is None:
                row = db.query(User).order_by(User.id).first()
            return row.username if row is not None else None
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Account management
    # ------------------------------------------------------------------

    def setup(self, username: str, password: str) -> bool:
        """First-run admin setup. Only works if no users exist."""
        with self._setup_lock:
            if self.is_configured:
                return False
            return self.create_user(username, password, is_admin=True)

    def create_user(self, username: str, password: str, is_admin: bool = False) -> bool:
        """Create a new user account."""
        from core.database import User

        username = normalize_username(username)
        if not username:
            return False
        db = self._db()
        try:
            if db.query(User).filter(User.username == username).first() is not None:
                return False
            db.add(User(
                public_id=generate_public_id("user"),
                username=username,
                password_hash=_hash_password(password),
                is_admin=bool(is_admin),
                privileges=dict(ADMIN_PRIVILEGES if is_admin else DEFAULT_PRIVILEGES),
            ))
            db.commit()
            logger.info("Created user '%s' (admin=%s)", username, is_admin)
            return True
        except Exception:
            db.rollback()
            logger.exception("Failed to create user '%s'", username)
            return False
        finally:
            db.close()

    def delete_user(self, username: str, requesting_user: str) -> bool:
        """Delete a user. Only admins can delete, and can't delete themselves.

        SECURITY: also revoke every active session token belonging to this
        user so any open browser tab they have gets kicked back to /login
        on the next request. Without this the user kept full access until
        their cookie expired naturally (default ~30 days).
        """
        from core.database import User, UserSession

        username = normalize_username(username)
        requesting_user = normalize_username(requesting_user)
        if not username or username == requesting_user:
            return False

        db = self._db()
        try:
            requester = self._get_user(db, requesting_user)
            target = self._get_user(db, username)
            if requester is None or target is None or not requester.is_admin:
                return False
            # Purge all sessions belonging to this user. validate_token also
            # cross-checks the user relationship, but deleting eagerly kicks
            # open browser tabs out on their next request.
            db.query(UserSession).filter(UserSession.user_id == target.id).delete(synchronize_session=False)
            db.delete(target)
            db.commit()
            logger.info("Deleted user '%s' (by %s)", username, requesting_user)
            return True
        except Exception:
            db.rollback()
            logger.exception("Failed to delete user '%s'", username)
            return False
        finally:
            db.close()

    def rename_user(self, old_username: str, new_username: str, requesting_user: str) -> bool:
        """Rename a user in auth storage. Admin only.

        Active sessions are tied to users.id, so they follow the renamed
        account without rewriting token records.
        """
        from core.database import User

        old_username = normalize_username(old_username)
        new_username = normalize_username(new_username)
        requesting_user = normalize_username(requesting_user)
        if not old_username or not new_username:
            return False

        db = self._db()
        try:
            requester = self._get_user(db, requesting_user)
            target = self._get_user(db, old_username)
            if requester is None or target is None or not requester.is_admin:
                return False
            if db.query(User).filter(User.username == new_username).first() is not None:
                return False
            target.username = new_username
            target.updated_at = datetime.utcnow()
            db.commit()
            logger.info("Renamed user '%s' -> '%s' (by %s)", old_username, new_username, requesting_user)
            return True
        except Exception:
            db.rollback()
            logger.exception("Failed to rename user '%s'", old_username)
            return False
        finally:
            db.close()

    def is_admin(self, username: str) -> bool:
        db = self._db()
        try:
            user = self._get_user(db, username)
            return bool(user and user.is_admin)
        finally:
            db.close()

    def list_users(self) -> List[Dict[str, Any]]:
        return [
            {"username": username, "is_admin": data.get("is_admin", False), "privileges": self.get_privileges(username)}
            for username, data in self.users.items()
        ]

    def get_privileges(self, username: str) -> Dict[str, Any]:
        """Get privileges for a user. Admins get all privileges."""
        db = self._db()
        try:
            user = self._get_user(db, username)
            if user is None:
                return dict(DEFAULT_PRIVILEGES)
            if user.is_admin:
                return dict(ADMIN_PRIVILEGES)
            # Merge stored privileges with defaults (in case new privileges were added)
            stored = user.privileges or {}
            return {**DEFAULT_PRIVILEGES, **stored}
        finally:
            db.close()

    def set_privileges(self, username: str, privileges: Dict[str, Any]) -> bool:
        """Update privileges for a user. Can't modify admin privileges."""
        username = normalize_username(username)
        db = self._db()
        try:
            user = self._get_user(db, username)
            if user is None or user.is_admin:
                return False  # admins always have full access
            current = self.get_privileges(username)
            # Only allow known privilege keys
            for key, value in privileges.items():
                if key in DEFAULT_PRIVILEGES:
                    current[key] = value
            user.privileges = current
            user.updated_at = datetime.utcnow()
            db.commit()
            logger.info("Updated privileges for '%s': %s", username, current)
            return True
        except Exception:
            db.rollback()
            logger.exception("Failed to update privileges for '%s'", username)
            return False
        finally:
            db.close()

    def change_password(self, username: str, current_password: str, new_password: str) -> bool:
        username = normalize_username(username)
        db = self._db()
        try:
            user = self._get_user(db, username)
            if user is None or not _verify_password(current_password, user.password_hash):
                return False
            user.password_hash = _hash_password(new_password)
            user.updated_at = datetime.utcnow()
            db.commit()
            return True
        except Exception:
            db.rollback()
            logger.exception("Failed to change password for '%s'", username)
            return False
        finally:
            db.close()

    # ------------------------------------------------------------------
    # TOTP two-factor authentication
    # ------------------------------------------------------------------

    def totp_enabled(self, username: str) -> bool:
        """Check if 2FA is enabled for a user."""
        db = self._db()
        try:
            user = self._get_user(db, username)
            return bool(user and user.totp_enabled)
        finally:
            db.close()

    def totp_generate_secret(self, username: str) -> Optional[str]:
        """Generate a new TOTP secret for a user. Returns the secret (not yet enabled)."""
        username = normalize_username(username)
        db = self._db()
        try:
            user = self._get_user(db, username)
            if user is None:
                return None
            secret = pyotp.random_base32()
            user.totp_secret_pending = secret
            user.updated_at = datetime.utcnow()
            db.commit()
            return secret
        except Exception:
            db.rollback()
            logger.exception("Failed to generate TOTP secret for '%s'", username)
            return None
        finally:
            db.close()

    def totp_get_provisioning_uri(self, username: str, secret: str) -> str:
        """Get the otpauth:// URI for QR code generation."""
        return pyotp.TOTP(secret).provisioning_uri(name=normalize_username(username), issuer_name="Odysseus")

    def totp_confirm_enable(self, username: str, code: str) -> bool:
        """Verify a TOTP code against the pending secret, then enable 2FA."""
        username = normalize_username(username)
        db = self._db()
        try:
            user = self._get_user(db, username)
            if user is None or not user.totp_secret_pending:
                return False
            if not pyotp.TOTP(user.totp_secret_pending).verify(code, valid_window=1):
                return False
            # Enable 2FA
            backup = [secrets.token_hex(4) for _ in range(8)]
            user.totp_secret = user.totp_secret_pending
            user.totp_secret_pending = None
            user.totp_enabled = True
            # Generate backup codes
            user.totp_backup_codes = backup
            user.updated_at = datetime.utcnow()
            db.commit()
            logger.info("2FA enabled for '%s'", username)
            return True
        except Exception:
            db.rollback()
            logger.exception("Failed to confirm TOTP for '%s'", username)
            return False
        finally:
            db.close()

    def totp_verify(self, username: str, code: str) -> bool:
        """Verify a TOTP code for login."""
        username = normalize_username(username)
        db = self._db()
        try:
            user = self._get_user(db, username)
            if user is None or not user.totp_enabled:
                return True  # 2FA not enabled, always pass
            if not user.totp_secret:
                return True
            # Check backup codes first
            backup = list(user.totp_backup_codes or [])
            if code in backup:
                backup.remove(code)
                user.totp_backup_codes = backup
                user.updated_at = datetime.utcnow()
                db.commit()
                logger.info("Backup code used for '%s' (%d remaining)", username, len(backup))
                return True
            return pyotp.TOTP(user.totp_secret).verify(code, valid_window=1)
        except Exception:
            db.rollback()
            logger.exception("Failed to verify TOTP for '%s'", username)
            return False
        finally:
            db.close()

    def totp_disable(self, username: str, password: str) -> bool:
        """Disable 2FA for a user. Requires password confirmation."""
        username = normalize_username(username)
        if not self.verify_password(username, password):
            return False
        db = self._db()
        try:
            user = self._get_user(db, username)
            if user is None:
                return False
            user.totp_secret = None
            user.totp_secret_pending = None
            user.totp_backup_codes = []
            user.totp_enabled = False
            user.updated_at = datetime.utcnow()
            db.commit()
            logger.info("2FA disabled for '%s'", username)
            return True
        except Exception:
            db.rollback()
            logger.exception("Failed to disable TOTP for '%s'", username)
            return False
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Login / logout / session tokens
    # ------------------------------------------------------------------

    def verify_password(self, username: str, password: str) -> bool:
        db = self._db()
        try:
            user = self._get_user(db, username)
            return bool(user and _verify_password(password, user.password_hash))
        finally:
            db.close()

    def create_session(self, username: str, password: str) -> Optional[str]:
        """Verify credentials and return a session token, or None."""
        from core.database import UserSession

        username = normalize_username(username)
        if not self.verify_password(username, password):
            return None
        token = secrets.token_hex(32)
        expires_at = datetime.utcnow() + timedelta(seconds=TOKEN_TTL)

        with self._sessions_lock:
            # Persist session tokens to app.db (atomic via commit, lock-guarded).
            db = self._db()
            try:
                user = self._get_user(db, username)
                if user is None:
                    return None
                db.add(UserSession(
                    token_hash=_hash_token(token),
                    user_id=user.id,
                    expires_at=expires_at,
                ))
                db.commit()
                return token
            except Exception:
                db.rollback()
                logger.exception("Failed to create session for '%s'", username)
                return None
            finally:
                db.close()

    def validate_token(self, token: Optional[str]) -> bool:
        if not token:
            return False
        from core.database import UserSession

        with self._sessions_lock:
            db = self._db()
            try:
                row = db.query(UserSession).filter(UserSession.token_hash == _hash_token(token)).first()
                if row is None:
                    return False
                if row.expires_at <= datetime.utcnow() or row.user is None:
                    # SECURITY: if the user record has since been removed
                    # (admin deleted them while their cookie was still valid),
                    # drop the session so the next request kicks them out
                    # instead of silently authenticating a non-existent account.
                    db.delete(row)
                    db.commit()
                    return False
                return True
            except Exception:
                db.rollback()
                logger.exception("Failed to validate auth token")
                return False
            finally:
                db.close()

    def get_username_for_token(self, token: Optional[str]) -> Optional[str]:
        """Return the username associated with a valid token."""
        if not token:
            return None
        from core.database import UserSession

        with self._sessions_lock:
            db = self._db()
            try:
                row = db.query(UserSession).filter(UserSession.token_hash == _hash_token(token)).first()
                if row is None:
                    return None
                if row.expires_at <= datetime.utcnow() or row.user is None:
                    # SECURITY: orphan check — same rationale as validate_token.
                    db.delete(row)
                    db.commit()
                    return None
                return row.user.username
            except Exception:
                db.rollback()
                logger.exception("Failed to resolve auth token")
                return None
            finally:
                db.close()

    def revoke_token(self, token: str):
        if not token:
            return
        from core.database import UserSession

        with self._sessions_lock:
            db = self._db()
            try:
                db.query(UserSession).filter(UserSession.token_hash == _hash_token(token)).delete(synchronize_session=False)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Failed to revoke auth token")
            finally:
                db.close()

    def status(self, token: Optional[str]) -> Dict[str, Any]:
        username = self.get_username_for_token(token)
        authenticated = username is not None
        result = {
            "configured": self.is_configured,
            "authenticated": authenticated,
            "username": username,
            "is_admin": self.is_admin(username) if username else False,
        }
        if authenticated:
            result["privileges"] = self.get_privileges(username)
        return result
