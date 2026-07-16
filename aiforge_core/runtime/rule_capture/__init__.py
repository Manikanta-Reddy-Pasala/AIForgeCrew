"""Deterministic, always-on Rule / Memory / Feedback capture.

Every user chat message is run through ONE capped LLM ``classify`` pass
(independent of which model the agent itself uses), so a directive, fact, or
correction the user states in passing is captured deterministically rather than
relying on the agent model choosing to call ``remember_rule``.

Pipeline (all in the chat handler, BEFORE the agent runs):

    classify(message) -> {category, scope, canonical, confidence, task_present}
        │  category != "none" and confidence >= threshold?
        ▼
    store(c)                  -> routes by category × scope (md_store / repo
                                 rules / AiForgeMemory / in-session store)
    recognize_gate_intent(c)  -> RECOGNIZES (does NOT set) a commit/delete
                                 gate-disable request, so the UI can OFFER an
                                 explicit, scoped, revocable opt-in

A gate is NEVER disabled by the classifier. Disabling one is a separate,
user-confirmed ``set_gate_flag`` call (the pill opt-in). ``flag_active`` ignores
chat-set flags entirely for autonomous runs (session_id is None).

Everything FAILS OPEN: any error in classify/store/recognition returns a safe
default and never raises into the chat turn. A capture must never break a chat.

See ``docs/superpowers/specs/2026-06-26-rule-memory-capture-design.md``.

This module was split (grouped by concern) into ``_base`` / ``_classify`` /
``_store`` / ``_gates`` / ``_transparency`` submodules; this package re-exports
the full former top-level surface so ``from aiforge_core.runtime import
rule_capture`` and every ``rule_capture.<name>`` attribute access is unchanged.
"""
from __future__ import annotations

from ._base import (
    _LOCK,
    _SESSION_ITEMS,
    _VALID_CATEGORIES,
    _VALID_SCOPES,
    _atomic_write,
    _config_dir,
    _disabled,
    _file_lock,
    _flags_path,
    _index_path,
    _load_flags,
    _load_index,
    _min_conf,
    _none,
    _save_flags,
    _save_index,
    fcntl,
    log,
    repo_key,
)
from ._classify import (
    _SYS,
    _extract_json,
    _llm_complete,
    _parse_classification,
    classify,
)
from ._gates import (
    _COMMIT_ACTIONS,
    _COMMIT_STRONG,
    _COMMIT_WEAK,
    _DELETE_ACTIONS,
    _DELETE_STRONG,
    _DELETE_WEAK,
    _GIT_HEAD_RE,
    _NEG_COMMIT_RE,
    _NEG_DELETE_RE,
    _NEG_GUARD,
    _SHELL_SEP_RE,
    _VALID_FLAGS,
    _clear_applied_flags,
    _intent,
    _prune_stale_session_flags,
    _record_applied_flag,
    clear_gate_flag,
    flag_active,
    flag_active_scope,
    GATE_INTENT_FLAG,
    is_commit_command,
    list_flags,
    recognize_gate_intent,
    set_gate_flag,
)
from ._store import (
    _do_store,
    _session_add,
    _slug,
    _write_repo_rule,
    store,
)
from ._transparency import (
    _ACTIONABLE_TIME_RE,
    _ACTIONABLE_VERB_RE,
    _GREETINGS,
    _has_cue,
    _remove_storage,
    list_captured,
    looks_actionable,
    rescope,
    should_classify,
    undo,
)

__all__ = [
    "classify", "store", "list_captured", "rescope", "undo",
    "recognize_gate_intent", "GATE_INTENT_FLAG",
    "is_commit_command", "repo_key",
    "set_gate_flag", "clear_gate_flag", "flag_active", "flag_active_scope",
    "list_flags", "should_classify", "looks_actionable",
]
