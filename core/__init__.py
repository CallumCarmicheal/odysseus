# core/__init__.py
"""
Chat Core — the essential chat experience.

This package contains only what's needed for:
- Streaming LLM responses
- Session management
- Model routing
- Authentication

Exports are loaded lazily so importing lightweight submodules such as
``core.platform_compat`` does not initialize the database or LLM stack.
"""

_EXPORTS = {
    # LLM
    "llm_call": ("src.llm_core", "llm_call"),
    "llm_call_async": ("src.llm_core", "llm_call_async"),
    "stream_llm": ("src.llm_core", "stream_llm"),
    "list_model_ids": ("src.llm_core", "list_model_ids"),
    "normalize_model_id": ("src.llm_core", "normalize_model_id"),
    "LLMConfig": ("src.llm_core", "LLMConfig"),
    # Auth
    "AuthManager": ("core.auth", "AuthManager"),
    # Middleware
    "SecurityHeadersMiddleware": ("core.middleware", "SecurityHeadersMiddleware"),
    # Exceptions
    "SessionNotFoundError": ("core.exceptions", "SessionNotFoundError"),
    "InvalidFileUploadError": ("core.exceptions", "InvalidFileUploadError"),
    "LLMServiceError": ("core.exceptions", "LLMServiceError"),
    "WebSearchError": ("core.exceptions", "WebSearchError"),
    # Models
    "Session": ("core.models", "Session"),
    "ChatMessage": ("core.models", "ChatMessage"),
    "SessionManager": ("core.session_manager", "SessionManager"),
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc

    from importlib import import_module

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
