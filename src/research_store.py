"""DB-backed deep-research result storage with legacy JSON fallback."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from core.identity import normalize_username, resolve_user_id

logger = logging.getLogger(__name__)

RESEARCH_DATA_DIR = Path("data/deep_research")
_RESEARCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def is_valid_research_id(research_id: str) -> bool:
    """Return True when *research_id* is safe for DB lookup and legacy paths."""
    return _RESEARCH_ID_RE.fullmatch(research_id or "") is not None


def _legacy_path(session_id: str) -> Path:
    if not is_valid_research_id(session_id):
        raise ValueError("Invalid research id")
    return RESEARCH_DATA_DIR / f"{session_id}.json"


def _load_legacy_json(session_id: str) -> Optional[dict]:
    try:
        path = _legacy_path(session_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("Failed to read legacy research JSON %s: %s", session_id, exc)
        return None


def _write_legacy_json(session_id: str, data: dict) -> None:
    path = _legacy_path(session_id)
    RESEARCH_DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _delete_legacy_json(session_id: str) -> None:
    try:
        path = _legacy_path(session_id)
    except ValueError:
        return
    if path.exists():
        path.unlink()


def _normalize_payload(session_id: str, data: dict) -> dict:
    payload = dict(data or {})
    payload.setdefault("query", "")
    payload.setdefault("status", "done")
    payload.setdefault("sources", [])
    payload.setdefault("raw_findings", [])
    payload.setdefault("started_at", 0)
    payload.setdefault("completed_at", time.time())
    payload.setdefault("owner", "")
    payload["id"] = session_id
    return payload


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _row_to_payload(row) -> dict:
    payload = dict(row.payload or {})
    payload.update({
        "id": row.public_id,
        "query": row.query or "",
        "status": row.status or "done",
        "result": row.result,
        "raw_report": row.raw_report or "",
        "sources": _as_list(row.sources),
        "raw_findings": _as_list(row.raw_findings),
        "stats": _as_dict(row.stats),
        "category": row.category,
        "started_at": row.started_at or 0,
        "completed_at": row.completed_at or 0,
        "owner": row.owner or "",
        "consumed": bool(row.consumed),
        "archived": bool(row.archived),
        "hidden_images": _as_list(row.hidden_images),
    })
    if row.task_id:
        payload["task_id"] = row.task_id
    if row.task_name:
        payload["task_name"] = row.task_name
    return payload


def _apply_payload(row, session_id: str, data: dict, db) -> None:
    from core.database import User

    payload = _normalize_payload(session_id, data)
    owner = normalize_username(payload.get("owner"))
    row.public_id = session_id
    row.owner = owner
    row.owner_id = resolve_user_id(db, User, owner)
    row.query = str(payload.get("query") or "")
    row.status = str(payload.get("status") or "done")
    row.result = payload.get("result")
    row.raw_report = payload.get("raw_report") or ""
    row.sources = _as_list(payload.get("sources"))
    row.raw_findings = _as_list(payload.get("raw_findings"))
    row.stats = _as_dict(payload.get("stats"))
    row.category = payload.get("category")
    row.started_at = _as_float(payload.get("started_at"))
    row.completed_at = _as_float(payload.get("completed_at"))
    row.consumed = bool(payload.get("consumed"))
    row.archived = bool(payload.get("archived"))
    row.hidden_images = _as_list(payload.get("hidden_images"))
    row.task_id = str(payload.get("task_id")) if payload.get("task_id") is not None else None
    row.task_name = payload.get("task_name")
    row.payload = payload


def save_research_result(session_id: str, data: dict, *, fallback_to_json: bool = True) -> bool:
    """Save a deep-research result to app.db, falling back to JSON if needed."""
    if not is_valid_research_id(session_id):
        raise ValueError("Invalid research id")
    payload = _normalize_payload(session_id, data)
    try:
        from core.database import ResearchResult, SessionLocal

        db = SessionLocal()
        try:
            row = db.query(ResearchResult).filter(ResearchResult.public_id == session_id).first()
            if row is None:
                row = ResearchResult(public_id=session_id)
                db.add(row)
            _apply_payload(row, session_id, payload, db)
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
        logger.warning("DB-backed research storage unavailable; falling back to JSON: %s", exc)
        _write_legacy_json(session_id, payload)
        return False


def load_research_result(session_id: str, *, import_legacy: bool = True) -> Optional[dict]:
    """Load a deep-research result from app.db, importing legacy JSON if needed."""
    if not is_valid_research_id(session_id):
        return None
    try:
        from core.database import ResearchResult, SessionLocal

        db = SessionLocal()
        try:
            row = db.query(ResearchResult).filter(ResearchResult.public_id == session_id).first()
            if row is not None:
                return _row_to_payload(row)

            legacy = _load_legacy_json(session_id)
            if legacy is None:
                return None
            if import_legacy:
                row = ResearchResult(public_id=session_id)
                db.add(row)
                _apply_payload(row, session_id, legacy, db)
                db.commit()
            return _normalize_payload(session_id, legacy)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as exc:
        logger.warning("DB-backed research read unavailable; falling back to JSON: %s", exc)
        return _load_legacy_json(session_id)


def update_research_result(session_id: str, changes: dict) -> bool:
    """Patch a persisted research result while preserving unmodeled payload fields."""
    data = load_research_result(session_id)
    if data is None:
        return False
    data.update(changes)
    save_research_result(session_id, data)
    return True


def mark_research_consumed(session_id: str) -> bool:
    """Mark a research result as consumed without deleting the saved report."""
    return update_research_result(session_id, {"consumed": True})


def set_research_archived(session_id: str, archived: bool) -> bool:
    """Soft-archive or restore a research result."""
    return update_research_result(session_id, {"archived": bool(archived)})


def delete_research_result(session_id: str) -> bool:
    """Delete a research result from app.db and remove any legacy JSON fallback."""
    if not is_valid_research_id(session_id):
        return False
    deleted = False
    try:
        from core.database import ResearchResult, SessionLocal

        db = SessionLocal()
        try:
            row = db.query(ResearchResult).filter(ResearchResult.public_id == session_id).first()
            if row is not None:
                db.delete(row)
                deleted = True
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as exc:
        logger.warning("DB-backed research delete unavailable; falling back to JSON delete: %s", exc)
    try:
        path = _legacy_path(session_id)
        if path.exists():
            path.unlink()
            deleted = True
    except Exception as exc:
        logger.warning("Failed to delete legacy research JSON %s: %s", session_id, exc)
    return deleted


def list_research_results(
    *,
    owner: Optional[str] = None,
    search: Optional[str] = None,
    archived: Optional[bool] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """List research results from app.db plus any legacy JSON rows not yet imported."""
    owner_key = normalize_username(owner) if owner is not None else None
    search_key = (search or "").lower()
    by_id: dict[str, dict] = {}

    try:
        from core.database import ResearchResult, SessionLocal

        db = SessionLocal()
        try:
            query = db.query(ResearchResult)
            if owner_key is not None:
                query = query.filter(ResearchResult.owner == owner_key)
            if archived is not None:
                query = query.filter(ResearchResult.archived == bool(archived))
            for row in query.all():
                payload = _row_to_payload(row)
                if search_key and search_key not in payload.get("query", "").lower():
                    continue
                by_id[payload["id"]] = payload
        finally:
            db.close()
    except Exception as exc:
        logger.warning("DB-backed research list unavailable; using legacy JSON only: %s", exc)

    try:
        for path in RESEARCH_DATA_DIR.glob("*.json"):
            session_id = path.stem
            if session_id in by_id:
                continue
            data = _load_legacy_json(session_id)
            if data is None:
                continue
            payload = _normalize_payload(session_id, data)
            if owner_key is not None and normalize_username(payload.get("owner")) != owner_key:
                continue
            if archived is not None and bool(payload.get("archived")) != bool(archived):
                continue
            if search_key and search_key not in payload.get("query", "").lower():
                continue
            save_research_result(session_id, payload)
            by_id[session_id] = payload
    except Exception as exc:
        logger.warning("Failed to scan legacy research JSON directory: %s", exc)

    items = sorted(by_id.values(), key=lambda item: item.get("completed_at") or 0, reverse=True)
    return items[:limit] if limit is not None else items


def average_completed_duration(items: Optional[Iterable[dict]] = None) -> Optional[float]:
    """Compute average completed research duration from persisted results."""
    durations = []
    for data in items if items is not None else list_research_results():
        if data.get("status") != "done":
            continue
        started = data.get("started_at", 0)
        completed = data.get("completed_at", 0)
        if started and completed and completed > started:
            durations.append(completed - started)
    return (sum(durations) / len(durations)) if durations else None
