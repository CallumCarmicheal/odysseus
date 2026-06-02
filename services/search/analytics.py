"""Search analytics, metrics tracking, and exception hierarchy."""

import logging
from collections import Counter
from pathlib import Path
from typing import Dict, Any

from src.search_analytics_store import default_search_analytics, load_search_analytics, save_search_analytics
from .cache import cache_metrics

logger = logging.getLogger(__name__)

# Dedicated error logger with file handler
_error_log_path = Path(__file__).resolve().parent.parent / "search_engine_error.log"
_error_handler = logging.FileHandler(_error_log_path, encoding="utf-8")
_error_handler.setLevel(logging.WARNING)
_error_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
error_logger = logging.getLogger("search_engine_error")
error_logger.addHandler(_error_handler)
error_logger.propagate = False

# Analytics file
# Legacy fallback/import source; app.db is the primary store.
ANALYTICS_FILE = Path(__file__).resolve().parent.parent / "search_analytics.json"


# ----------------------------------------------------------------------
# Custom exception hierarchy
# ----------------------------------------------------------------------
class SearchEngineError(Exception):
    """Base class for all search-engine related errors."""


class NetworkError(SearchEngineError):
    """Raised when a network request fails (e.g., timeout, DNS error)."""


class ParseError(SearchEngineError):
    """Raised when HTML or other content cannot be parsed."""


class RateLimitError(SearchEngineError):
    """Raised when the remote service returns a rate-limit (HTTP 429)."""


# ----------------------------------------------------------------------
# Analytics helpers
# ----------------------------------------------------------------------
def _load_analytics() -> Dict[str, Any]:
    """Load analytics data from app.db, creating defaults if missing."""
    try:
        return load_search_analytics(legacy_paths=[ANALYTICS_FILE])
    except Exception as e:
        logger.warning(f"Failed to load analytics data: {e}")
        return default_search_analytics()


def _save_analytics(data: Dict[str, Any]) -> None:
    """Persist analytics data to app.db, falling back to the JSON file."""
    try:
        save_search_analytics(data, legacy_path=ANALYTICS_FILE)
    except Exception as e:
        logger.warning(f"Failed to write analytics data: {e}")


def _record_query(query: str, success: bool, cache_hit: bool) -> None:
    """Update analytics for a single query execution."""
    analytics = _load_analytics()
    analytics["total_queries"] += 1
    if success:
        analytics["successful_queries"] += 1
    else:
        analytics["failed_queries"] += 1

    if cache_hit:
        analytics["cache_hits"] += 1
        cache_metrics["hits"] += 1
    else:
        analytics["cache_misses"] += 1
        cache_metrics["misses"] += 1

    patterns = analytics["query_patterns"]
    entry = patterns.get(query, {"count": 0, "successes": 0})
    entry["count"] += 1
    if success:
        entry["successes"] += 1
    patterns[query] = entry

    _save_analytics(analytics)


def get_search_stats() -> Dict[str, Any]:
    """Return aggregated search analytics."""
    analytics = _load_analytics()
    total = analytics.get("total_queries", 0) or 1
    success_rate = analytics.get("successful_queries", 0) / total
    cache_total = analytics.get("cache_hits", 0) + analytics.get("cache_misses", 0) or 1
    cache_hit_rate = analytics.get("cache_hits", 0) / cache_total

    pattern_counter = Counter({
        q: data["count"] for q, data in analytics.get("query_patterns", {}).items()
    })
    most_common = [q for q, _ in pattern_counter.most_common(5)]

    return {
        "most_common_queries": most_common,
        "success_rate": success_rate,
        "cache_hit_rate": cache_hit_rate,
        "total_queries": analytics.get("total_queries", 0),
        "successful_queries": analytics.get("successful_queries", 0),
        "failed_queries": analytics.get("failed_queries", 0),
        "cache_hits": analytics.get("cache_hits", 0),
        "cache_misses": analytics.get("cache_misses", 0),
        "cache_evictions": cache_metrics["evictions"],
        "runtime_cache_hits": cache_metrics["hits"],
        "runtime_cache_misses": cache_metrics["misses"],
    }
