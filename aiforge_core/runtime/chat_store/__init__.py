"""Persistent chat sessions for the Claude-style multi-conversation UI.

SQLite-only build: the public function API (create_session, add_message,
get_messages, search_messages, …) is backed by the embedded SQLite impl.
The SQLite DB lives at ``$AIFORGE_CHAT_DB_PATH`` (default
``$AIFORGE_CONFIG_DIR/chat.db``).

This module was split (grouped by concern) into ``_helpers`` / ``_sqlite`` /
``_api`` submodules; this package re-exports the full public surface so
``from aiforge_core.runtime import chat_store`` and every
``chat_store.<name>`` attribute access is unchanged.
"""
from __future__ import annotations

from ._api import (
    _BACKEND_LOCK,
    _backend,
    add_media,
    add_message,
    create_session,
    dedupe_sessions,
    delete_all_sessions,
    delete_media,
    delete_messages_from,
    delete_session,
    get_media,
    get_messages,
    get_session,
    list_media,
    list_sessions,
    message_checkpoint,
    rename_session,
    reset_backend_for_tests,
    search_messages,
    set_media_description,
    set_message_checkpoint,
    set_session_cwd,
    set_session_role,
)
from ._helpers import (
    _iso,
    _media_out,
    _message_out,
    _rank_search,
    _session_out,
    _tokens,
)
from ._sqlite import _NOW, _SQLITE_DDL, _SqliteChatStore

__all__ = [
    "create_session", "set_session_cwd", "set_session_role", "list_sessions",
    "get_session", "get_messages", "search_messages", "add_message",
    "set_message_checkpoint", "delete_messages_from", "message_checkpoint",
    "rename_session", "delete_session", "delete_all_sessions", "dedupe_sessions",
    "add_media", "list_media", "get_media", "set_media_description", "delete_media",
    "reset_backend_for_tests",
]
