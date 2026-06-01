"""Helpers for separating internal integer IDs from external public IDs."""

from __future__ import annotations

import uuid
from typing import Any


def generate_public_id(prefix: str | None = None) -> str:
    """Generate a new external identifier for URLs and API payloads."""

    value = uuid.uuid4().hex
    clean_prefix = (prefix or "").strip().strip("_")
    return f"{clean_prefix}_{value}" if clean_prefix else value


def normalize_public_id(value: Any) -> str | None:
    """Normalize an externally supplied ID value."""

    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def public_id_from_legacy(value: Any, *, prefix: str | None = None) -> str:
    """Preserve an old string primary key as the new public ID when present."""

    return normalize_public_id(value) or generate_public_id(prefix)


def external_id_for(row: Any) -> str:
    """Return the ID that should leave the database boundary."""

    public_id = normalize_public_id(getattr(row, "public_id", None))
    if public_id:
        return public_id
    return str(getattr(row, "id"))


def ensure_public_id(row: Any, *, legacy_id: Any = None, prefix: str | None = None) -> str:
    """Assign and return ``row.public_id`` when the object supports it."""

    if not hasattr(row, "public_id"):
        raise AttributeError("row does not have a public_id attribute")
    public_id = normalize_public_id(getattr(row, "public_id", None))
    if not public_id:
        public_id = public_id_from_legacy(legacy_id, prefix=prefix)
        setattr(row, "public_id", public_id)
    return public_id


def filter_by_external_id(query: Any, model_cls: Any, value: Any) -> Any:
    """Filter a SQLAlchemy query by public ID, with integer-ID fallback.

    This is intended for transition-safe route code: old and future clients use
    public IDs, while internal follow-up tools may already hold integer IDs.
    """

    public_id = normalize_public_id(value)
    if public_id is None:
        raise ValueError("external ID is required")

    conditions = []
    if hasattr(model_cls, "public_id"):
        conditions.append(model_cls.public_id == public_id)
    if public_id.isdigit() and hasattr(model_cls, "id"):
        conditions.append(model_cls.id == int(public_id))
    if not conditions:
        raise AttributeError("model has neither public_id nor id")
    if len(conditions) == 1:
        return query.filter(conditions[0])

    from sqlalchemy import or_

    return query.filter(or_(*conditions))
