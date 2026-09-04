"""Reconcile/rewrite/self-heal: steering, patches, stubs, drift, integration.

Split from ``parallel_subtasks.py`` (mechanical move, behaviour identical).

This package re-exports every top-level name (public and underscore-private)
that the original flat ``_reconcile.py`` exposed, so ``from ._reconcile import
<name>`` keeps working for ``parallel_subtasks.__init__`` and the sibling
submodules. Concerns are grouped into ``_testrun`` (test/build run + failure
classification), ``_sources`` (source gathering + baseline/off-plan — holds the
duplicated ``_SRC_EXTS``), ``_rewrite`` (patch resolver), ``_scaffold`` (stubs +
decomposition), ``_drift`` (dead-import prune + symbol drift), and
``_integration`` (the reconcile loop + spec render/verify).

The original module's top-level imports are preserved verbatim below to keep the
import-time side effects (and thus behaviour) byte-identical."""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import subprocess
import threading

from pydantic import BaseModel

from aiforge_core.runtime import review_gates
from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS, ensure_artifact_gitignore

from ._testrun import (
    _broken_project_config,
    _collect_run_output,
    _directed_hints,
    _escalation_model,
    _fail_count,
    _is_hard_residual,
    _project_test_output,
    _raw_build_test_output,
    _reconcile_rounds,
    _route_steering,
)
from ._sources import (
    _BASELINE_FILE,
    _SRC_EXTS,
    _baseline_set,
    _change_in_error,
    _files_in_output,
    _gather_sources,
    _is_greenfield,
    _prune_offplan_files,
    _py_local_imports,
    _relevant_files,
    _snapshot_baseline,
    _spec_declared_paths,
    _spec_goal,
)
from ._scaffold import (
    _COMMENT_PREFIX,
    _NON_MODULE_TEST_STEMS,
    _SCAFFOLD_MARK,
    _enforce_disjoint_files,
    _ensure_impl_modules,
    _impl_path_for_test,
    _python_stub,
    _scaffold_stubs,
    _stub_content,
)
from ._drift import (
    _prune_dead_python_imports,
    _symbol_drift_report,
)
from ._rewrite import (
    _file_headers,
    _patches,
    _apply_patches,
    _rewrite_fix,
)
from ._integration import (
    _reconcile_integration,
    _render_spec_md,
    _verify_against_spec,
)
