"""DB-backed search analytics counters with legacy JSON fallback."""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

SEARCH_ANALYTICS_KEY = "search_analytics"
_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_LEGACY_PATHS = (
    _ROOT / "src" / "search_analytics.json",
    _ROOT / "services" / "search_analytics.json",
    Path(os.environ.get("DATA_DIR", "data")) / "search_analytics.json",
)


def default_search_analytics() -> dict:
    """Return the default search analytics payload."""
    return {
        "total_queries": 0,
        "successful_queries": 0,
        "failed_queries": 0,
        "cache_hits": 0,
        "cache_misses": 0,
        "query_patterns": {},
    }


def _normalize(data: Any) -> dict:
    payload = default_search_analytics()
    if not isinstance(data, dict):
        return payload
    for key in ("total_queries", "successful_queries", "failed_queries", "cache_hits", "cache_misses"):
        try:
            payload[key] = int(data.get(key) or 0)
        except (TypeError, ValueError):
            payload[key] = 0
    patterns = data.get("query_patterns")
    if isinstance(patterns, dict):
        normalized_patterns = {}
        for query, raw in patterns.items():
            if not isinstance(raw, dict):
                continue
            try:
                count = int(raw.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            try:
                successes = int(raw.get("successes") or 0)
            except (TypeError, ValueError):
                successes = 0
            normalized_patterns[str(query)] = {"count": count, "successes": successes}
        payload["query_patterns"] = normalized_patterns
    return payload


def _merge(left: dict, right: dict) -> dict:
    merged = _normalize(left)
    incoming = _normalize(right)
    for key in ("total_queries", "successful_queries", "failed_queries", "cache_hits", "cache_misses"):
        merged[key] += incoming[key]
    patterns = merged.setdefault("query_patterns", {})
    for query, raw in incoming.get("query_patterns", {}).items():
        entry = patterns.setdefault(query, {"count": 0, "successes": 0})
        entry["count"] += raw.get("count", 0)
        entry["successes"] += raw.get("successes", 0)
    return merged


def _legacy_paths(extra_paths: Iterable[Path] | None = None) -> list[Path]:
    seen = set()
    paths = []
    for path in list(extra_paths or []) + list(_DEFAULT_LEGACY_PATHS):
        resolved = Path(path)
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            paths.append(resolved)
    return paths


def _load_legacy(paths: Iterable[Path] | None = None) -> dict:
    payload = default_search_analytics()
    found = False
    for path in _legacy_paths(paths):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            payload = _merge(payload, data)
            found = True
        except Exception as exc:
            logger.warning("Failed to load legacy search analytics file %s: %s", path, exc)
    return payload if found else default_search_analytics()


def _write_legacy(data: dict, path: Path | None = None) -> None:
    from core.atomic_io import atomic_write_json

    target = path or (Path(os.environ.get("DATA_DIR", "data")) / "search_analytics.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(target), _normalize(data), indent=2)


def load_search_analytics(*, legacy_paths: Iterable[Path] | None = None, import_legacy: bool = True) -> dict:
    """Load search analytics from app.db, importing legacy JSON if needed."""
    try:
        from core.database import AppSetting, SessionLocal

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SEARCH_ANALYTICS_KEY).first()
            if row is not None:
                return _normalize(copy.deepcopy(row.value))

            legacy = _load_legacy(legacy_paths)
            if import_legacy:
                db.add(AppSetting(key=SEARCH_ANALYTICS_KEY, value=legacy))
                db.commit()
            return legacy
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as exc:
        logger.warning("DB-backed search analytics unavailable; falling back to JSON: %s", exc)
        return _load_legacy(legacy_paths)


def save_search_analytics(
    data: dict,
    *,
    legacy_path: Path | None = None,
    fallback_to_json: bool = True,
) -> bool:
    """Save search analytics to app.db, falling back to JSON if needed."""
    payload = _normalize(data)
    try:
        from core.database import AppSetting, SessionLocal

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SEARCH_ANALYTICS_KEY).first()
            if row is None:
                db.add(AppSetting(key=SEARCH_ANALYTICS_KEY, value=payload))
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
        logger.warning("DB-backed search analytics unavailable; falling back to JSON: %s", exc)
        _write_legacy(payload, legacy_path)
        return False
