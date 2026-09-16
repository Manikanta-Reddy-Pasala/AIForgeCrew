"""Chat routes (/api/chat/*) — split out of api.py (APIRouter).

The code lives in ``routes/_chat/``, one module per job; this module
re-exports every name it used to define, so ``chat.<name>`` keeps working.
Patch a helper in the ``_chat`` module that calls it. The file itself must
stay: the frozen ``aiforge`` CLI recognises a checkout by this path.
"""
from __future__ import annotations

# Imports the single module exposed; tests reach some through it.
from aiforge_core.config import agent_config as _acfg  # noqa: F401
from aiforge_core.config import model_registry as _model_registry  # noqa: F401
from aiforge_core.config.paths import config_dir  # noqa: F401
from aiforge_core.runtime.background import spawn as _spawn  # noqa: F401
from aiforge_core.tickets import store as tickets_mod  # noqa: F401

from ._chat import (
    _core,  # noqa: F401
    _history,  # noqa: F401
    _message,  # noqa: F401
    _models,  # noqa: F401
    _prep,  # noqa: F401
    _producer,  # noqa: F401
    _routing,  # noqa: F401
    _sessions,  # noqa: F401
    _stages,  # noqa: F401
    _turn_events,  # noqa: F401
)
from ._chat._core import (  # noqa: F401
    _NEW_CHAT,
    _PRODUCE_SEM,
    _af_log,
    _ApprovalModeBody,
    _ChatAgentBody,
    _ChatAskBody,
    _ChatMessage,
    _default_cwd,
    _NewSessionBody,
    _request_repo_root,
    approval_settings_get,
    approval_settings_set,
    chat_agent,
    chat_ask,
    chat_retain,
    router,
)
from ._chat._history import (  # noqa: F401
    _DIGEST_ARG_KEYS,
    _TERMINAL_SUBTASK,
    _TOPIC_CUE_PHRASES,
    _bind_turn_meter,
    _capture_chat_cue,
    _chat_history_for_agent,
    _chat_learn_writeback,
    _chat_summarize_session,
    _history_row_content,
    _maybe_downgrade_team,
    _note_staleness_notice,
    _setup_chat_logger,
    _step_arg,
    _step_digest,
    _step_mark,
    _warn_if_not_persisted,
)
from ._chat._message import (  # noqa: F401
    _ApproveBody,
    _CheckpointBody,
    _RestoreBody,
    _SessionTicketBody,
    _SteerBody,
    chat_kill_all,
    chat_session_approve,
    chat_session_attach,
    chat_session_checkpoint_create,
    chat_session_checkpoint_restore,
    chat_session_checkpoints,
    chat_session_message,
    chat_session_steer,
    chat_session_stop,
    chat_session_ticket,
    suggestion_history,
    suggestion_outcome,
)
from ._chat._models import (  # noqa: F401
    _ORCHESTRATOR_ROLES,
    _chat_capable,
    _ChatModelBody,
    _endpoint_for_picked_model,
    _env_pin_warning,
    _merge_registry_and_served,
    _model_env_override,
    _ModelReloadBody,
    _registry_models,
    _served_model_ids,
    _served_model_ids_for_role,
    _url_key,
    chat_model_reload,
    chat_model_set,
    chat_models,
    orchestrator_model_get,
    orchestrator_model_set,
)
from ._chat._prep import (  # noqa: F401
    _apply_edit_resend,
    _apply_provisional_title,
    _apply_resume_brief,
    _auto_checkpoint,
    _expand_slash_command,
    _gen_title,
    _rehome_context_workspace,
    _with_resume,
)
from ._chat._producer import (  # noqa: F401
    _events,
    _produce,
    _stream,
)
from ._chat._routing import (  # noqa: F401
    _decide_chat_route,
    _doc_task_route,
    _pipeline_route,
    _plan_mode_route,
    _rule_capture_pass,
    _run_capture_pass,
    _should_skip_enhance,
)
from ._chat._sessions import (  # noqa: F401
    _chat_workspace_root,
    _delete_chat_workspace,
    _is_isolated_workspace,
    _MediaDescBody,
    _quick_step_cap,
    _RenameBody,
    _SessionMsgBody,
    _sweep_orphan_session_dirs,
    chat_media_delete,
    chat_media_describe,
    chat_media_list,
    chat_media_raw,
    chat_media_upload,
    chat_session_compact,
    chat_session_create,
    chat_session_delete,
    chat_session_get,
    chat_session_list,
    chat_session_llm_usage,
    chat_session_rename,
    chat_session_spec,
    chat_session_trace,
    chat_sessions_reset,
)
from ._chat._stages import (  # noqa: F401
    _DRAFT_ONLY_NOTE,
    _PUBLISH_INTENT_RE,
    _SRC_EXTS_VERIFY,
    _augment_user_turn,
    _commit_simple_baseline,
    _dispatch_agent_route,
    _early_route_events,
    _enhance_prompt,
    _fold_enriched_history,
    _integration_verify_events,
    _looks_like_analysis,
    _post_run_events,
    _prelude_notices,
    _single_agent_events,
    _turn_wrote_source,
    _worth_verifying,
)
from ._chat._turn_events import (  # noqa: F401
    _clean_and_log_produce_event,
    _consume_produce_events,
    _drive_produce_stream,
    _finalize_produce_turn,
    _mirror_event_to_log,
    _persist_produce_turn,
    _publish_final_usage,
    _reset_turn_context,
    _route_produce_event,
    _TurnResetContext,
    _usage_step_text,
)
