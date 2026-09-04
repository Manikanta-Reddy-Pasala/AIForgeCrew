"""Parallel multi-agent execution of a ticket's subtasks.

When a ticket is decomposed into subtickets, this runs each one CONCURRENTLY in
its OWN git worktree (isolation), updating the live subtask status, then merges
the successful branches back into the ticket's working branch in order.

Default ON: ``AIFORGE_PARALLEL_SUBTASKS=0`` disables (operator decision
2026-07-09). Concurrency capped by ``AIFORGE_PARALLEL_SUBTASKS_MAX`` (default 4)
(default 4).

The per-subtask executor is INJECTED (``run_one``) so the orchestration —
worktree isolation, concurrency, status tracking, sequential merge, conflict
handling, aggregation — is independently testable with real git. ``run_one``
receives ``(subtask, worktree_path)`` and must leave its work committed on the
worktree's branch; it returns ``{ok: bool, ...}``.
"""
from __future__ import annotations

from ._worktree import (
    log,
    _GIT_LOCK,
    enabled,
    _max_workers,
    _git,
    _slugify,
    _branch_for,
    _make_worktree,
    _commit_all,
    _retries,
    _reset_worktree,
    _attempt,
    _run_subtask,
    _build_or_test,
    default_validate_one,
    default_integration_test,
    _emit,
    _update,
    _dirty_warning,
    _conflict_hunks,
    _hunk_breadcrumbs,
    _resolve_conflict_hunk,
    _resolve_file_conflicts,
    _resolve_conflicts,
    _merge_branch,
)
from ._orchestrate import (
    _existing_source_digest,
    _sequential_order,
    _run_sequential,
    _shared_worktree_enabled,
    _GOAL_FILE_RE,
    _files_of,
    schedule_waves,
    _recurse_max,
    _decomp_retries,
    _run_one_recursive,
    _run_wave_set,
    _run_shared_worktree,
    run_parallel,
)
from ._runners import (
    default_run_one,
    _enforce_target_path,
    _FILE_BLOCK_RE,
    _parse_file_blocks,
    _in_scope,
    lightweight_run_one,
    _default_subtask_runner,
    _INFLIGHT,
    _INFLIGHT_LOCK,
    run_subtasks_parallel,
)
from ._planning import (
    _DECOMPOSE_SYS,
    _ENHANCE_SYS,
    _orchestrator_timeout_s,
    _enhancer_disabled,
    _enhancer_min_chars,
    _CONVERSATIONAL,
    _whole_conversational,
    _is_trivial_prompt,
    _ACTION_VERBS,
    _VERB_RE,
    _names_a_code_file,
    _MULTIPART_RE,
    _enhancer_skip_concrete_enabled,
    _is_concrete_prompt,
    _memory_block,
    _history_block,
    _readme_block,
    _enhance,
    _spec_degenerate,
    enhance,
    _ARCHITECT_SYS,
    _architect_context,
    _PLAN_CODE_EXTS,
    _validate_plan,
    _ArchFileSpec,
    _ArchitectPlan,
    _architect,
    _plan_files,
    _decompose,
    _ensure_git_workspace,
    _is_managed_workspace,
    _commit_turn_baseline,
)
from ._stream import (
    stream_parallel_team,
    _emit_changes,
    _to_int,
    _USER_MANDATES,
    _CHANGES_HIDE,
    _CODE_EXTS,
    _test_path_for,
    _ensure_test_coverage,
)
from ._contracts import (
    _CONTRACT_DIR,
    _path_to_module,
    _write_contract_sidecar,
    _DECL_KEYWORDS,
    _clean_symbol,
    _blackboard_from_contracts,
    _is_test_subtask,
    _matching_tests_for,
    _merge_aggs,
)
from ._reconcile import (
    _reconcile_rounds,
    _escalation_model,
    _collect_run_output,
    _raw_build_test_output,
    _project_test_output,
    _route_steering,
    _is_hard_residual,
    _directed_hints,
    _SRC_EXTS,
    _gather_sources,
    _spec_goal,
    _files_in_output,
    _py_local_imports,
    _relevant_files,
    _patches,
    _file_headers,
    _apply_patches,
    _rewrite_fix,
    _SCAFFOLD_MARK,
    _COMMENT_PREFIX,
    _stub_content,
    _python_stub,
    _NON_MODULE_TEST_STEMS,
    _impl_path_for_test,
    _enforce_disjoint_files,
    _ensure_impl_modules,
    _BASELINE_FILE,
    _snapshot_baseline,
    _baseline_set,
    _is_greenfield,
    _spec_declared_paths,
    _prune_offplan_files,
    _scaffold_stubs,
    _fail_count,
    _change_in_error,
    _prune_dead_python_imports,
    _symbol_drift_report,
    _reconcile_integration,
    _broken_project_config,
    _render_spec_md,
    _verify_against_spec,
)

__all__ = ["run_parallel", "run_subtasks_parallel", "default_run_one",
           "default_validate_one", "default_integration_test",
           "stream_parallel_team", "enabled"]
