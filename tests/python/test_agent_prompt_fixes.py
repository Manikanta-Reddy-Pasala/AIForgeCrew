"""Guards for the adversarial agent-prompt/config audit fixes.

These lock in the LOCAL-MODEL, multi-language (Java/Node/Go/Python)
corrections so a future "consistency" edit can't silently regress them:

* Fix A — Doer prompt is language-agnostic (no hardcoded `python -c
  'import app'` mandate) and drives its compile check from TOOLCHAIN;
  exactly ONE stop-budget rule remains.
* Fix B — agents.yaml is honest: Feedback is a text protocol (not JSON),
  and the Doer `forbidden` list no longer bans its real file surface.
* Fix C — the Researcher prompt names `repo_map` as its orientation step.
* Fix D — live_verifier keeps the real `grep` tool, not `file_grep`.
"""
from __future__ import annotations

import inspect
import pathlib

import yaml

from aiforge_core.runtime import prompts
from aiforge_core.runtime import prompts_extended as pe

_AGENTS_YAML = (
    pathlib.Path(prompts.__file__).resolve().parents[2]
    / "agents" / "agents.yaml"
)


# ── Fix A — de-Pythoned Doer prompt + unified stop budget ─────────────────


def test_doer_prompt_has_no_hardcoded_python_import_mandate():
    assert "python -c 'import app" not in prompts.DOER, (
        "Doer prompt still mandates the Python-only `python -c 'import "
        "app...'` check — must be language-agnostic")


def test_doer_prompt_drives_compile_check_from_toolchain():
    p = prompts.DOER
    assert "compile/import check from TOOLCHAIN" in p
    assert "compile_cmd" in p


def test_doer_prompt_has_single_stop_budget():
    p = prompts.DOER
    # the two old conflicting budgets are gone
    assert "3 attempts" not in p
    assert "tried twice" not in p
    # exactly one unified stop rule survives
    assert "Stop budget (ONE rule)" in p
    assert "fails twice in a row" in p


# ── Fix C — Researcher gains repo_map ─────────────────────────────────────


def test_researcher_prompt_mentions_repo_map():
    assert "repo_map" in pe.RESEARCHER


# ── Fix D — live_verifier keeps `grep`, not the phantom `file_grep` ───────


def test_live_verifier_keepset_uses_grep_not_file_grep():
    from aiforge_core.agents import live_verifier

    src = inspect.getsource(live_verifier._tools_factory)
    assert "file_grep" not in src
    assert '"grep"' in src


# ── Fix B — agents.yaml honesty ───────────────────────────────────────────


def _load_agents() -> dict:
    with open(_AGENTS_YAML, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_agents_yaml_parses():
    data = _load_agents()
    assert "agents" in data and "doer" in data["agents"]


def test_feedback_contract_describes_text_protocol_not_json():
    fb = _load_agents()["agents"]["feedback"]
    rule = fb["rule"]
    assert "TEXT" in rule and "NOT JSON" in rule
    # the old "single JSON verdict" / "valid JSON" claim is gone
    assert "single JSON verdict" not in rule
    joined = " ".join(str(x) for x in fb["termination_contract"])
    assert "valid JSON" not in joined


def test_doer_forbidden_no_longer_bans_real_file_surface():
    doer = _load_agents()["agents"]["doer"]
    forbidden = set(doer["tools"]["forbidden"])
    for real in ("file_write", "file_patch", "file_read", "list_dir", "run_shell"):
        assert real not in forbidden, f"{real} is a real Doer tool, not forbidden"


def test_doer_allowed_has_no_phantom_tools():
    """The phantom (chat-agent, not pipeline-Doer) tools are gone."""
    doer = _load_agents()["agents"]["doer"]
    allowed = set(doer["tools"]["allowed"])
    for phantom in (
        "browse", "execute_ipython_cell", "delegate_to_agent", "mcp",
        "memory_write", "format", "typecheck", "run_tests", "lsp",
        "update_working_checkpoint",
    ):
        assert phantom not in allowed, f"{phantom} is not wired to the Doer factory"
