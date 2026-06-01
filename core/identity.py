"""Identity helpers for the username-to-owner_id migration."""

from __future__ import annotations

from typing import Any


def normalize_username(username: Any) -> str:
    """Normalize usernames the same way auth currently stores them."""

    return str(username or "").strip().lower()


def is_shared_owner(owner: Any) -> bool:
    """Return true for legacy/shared ownership values."""

    return normalize_username(owner) == ""


def resolve_user_id(db: Any, user_model: Any, username: Any) -> int | None:
    """Resolve a username to ``users.id`` without importing app models."""

    normalized = normalize_username(username)
    if not normalized:
        return None
    row = db.query(user_model).filter(user_model.username == normalized).first()
    return getattr(row, "id", None) if row is not None else None


def require_user_id(db: Any, user_model: Any, username: Any) -> int:
    """Resolve a username and raise when the user row is missing."""

    user_id = resolve_user_id(db, user_model, username)
    if user_id is None:
        raise LookupError(f"User not found: {normalize_username(username)}")
    return int(user_id)
