"""Production ticket processor — single-shot ADK pipeline driver.

Polls Postgres for a ``todo`` ticket via
:func:`aiforge_core.tickets.store.claim_next_any`, runs one pass of the
v6 SequentialAgent (see :mod:`pipeline`), then exits. systemd
``Restart=always RestartSec=10`` keeps the loop polling.

Heavy lifting lives in sibling modules so this file stays a thin
orchestrator:

* :mod:`pipeline`     — agent factory + EscalatingLlm wiring
* :mod:`memory_block` — pre-flight AiForgeMemory recall
* :mod:`git_pr`       — auto-commit + push + open-PR helper
* :mod:`prompts`      — per-archetype instruction strings
* :mod:`doer_tools`   — file_read / file_write / run_shell / …

Invoke::

    python -m aiforge_core.runtime.adk_runner

This module was split (grouped by concern) into ``_base`` / ``_workspace`` /
``_verdict`` / ``_pipeline`` / ``_orchestrate`` submodules; this package
re-exports the full former public surface so ``from aiforge_core.runtime
import adk_runner`` and every ``adk_runner.<name>`` attribute access is
unchanged. The ``python -m`` entrypoint lives in ``__main__``.
"""
from __future__ import annotations

# Re-exported top-level imports the original module carried (kept so
# ``adk_runner.<name>`` continues to resolve for these too).
from .. import memory_block
from ..git_pr import commit_push_open_pr
from ..pipeline import build_pipeline, set_force_provider
from ..researcher_routing import should_skip_researcher

from ._base import (
    _REASON_DEFAULT_FAIL,
    _REASON_DEFAULT_PASS,
    _REASON_MAX_CHARS,
    _VERDICT_TO_STATUS,
    _VERDICT_TOKENS,
    log,
    tickets_mod,
)
from ._workspace import (
    _materialize_attachments_in_worktree,
    _persist_ticket_media,
    _restore_env,
    _setup_ticket_workspace,
)
from ._verdict import (
    _enhancer_block_reason,
    _extract_live_verifier,
    _extract_reason,
    _extract_verdict,
    _extract_verifier,
    _pipeline_deadline_s,
    _record_verdict_event,
    _ticket_looks_readonly,
)
from ._pipeline import (
    _build_context_plugins,
    _emit_ambiguous_rule_notice,
    _run_live_verifier,
    _run_pipeline,
    _run_single_agent,
)
from ._orchestrate import (
    _build_prompt,
    _ingest_ticket_external_refs,
    _process_one_ticket,
    _ticket_force_provider,
    main,
)

__all__ = ["main"]
