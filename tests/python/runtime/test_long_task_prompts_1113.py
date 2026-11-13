"""Prompt clean-ups for long coding tasks (tk-toml11 / click prompt dump, 11-13)."""
from __future__ import annotations

from aiforge_core.api.routes._chat import _stages
from aiforge_core.runtime.chat_agent._context import _recall
from aiforge_core.runtime.chat_agent._tools import _schemas

SPEC = """# tomlkit: support TOML v1.1.0

Update the parser so it accepts TOML v1.1.0 documents.

1. **Inline tables may span lines.** Newlines, comments and a trailing comma
   are allowed inside the braces.
2. **Seconds are optional** in date-times: `07:32` means `:00` seconds.
3. **New escapes in basic strings:** `\\e` and `\\xHH` (two hex digits, a code point
   up to U+00FF).
"""


# ── the restatement is attached only when it adds something ─────────────────
def test_a_restatement_that_copies_the_spec_is_dropped():
    m = {"role": "user", "content": SPEC}
    copy = " ".join(SPEC.split())            # same words, line breaks merged
    _stages._augment_user_turn(m, copy, None, SPEC, False)
    assert "[Interpreted request" not in m["content"]
    assert m["content"].strip() == SPEC.strip()


def test_a_restatement_with_real_context_is_kept():
    raw = "fix the login bug"
    rich = ("Fix the login bug: the session cookie set by auth/session.py "
            "expires immediately because max_age is passed in milliseconds.")
    m = {"role": "user", "content": raw}
    _stages._augment_user_turn(m, rich, None, raw, False)
    assert "[Interpreted request" in m["content"] and "max_age" in m["content"]


# ── built-in rules are gated by their triggers ─────────────────────────────
def test_shipped_jira_rules_stay_out_of_a_parser_task(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent._tools import _memory
    monkeypatch.setattr(_memory, "_md_rule_bullets", lambda cwd: ([], []))
    monkeypatch.setattr(_memory, "_constraint_bullets", lambda cwd: [])
    out = _memory._rules_context(str(tmp_path), "Update the TOML parser to "
                                 "accept v1.1.0 inline tables and escapes")
    assert "Jira" not in out and "CLR" not in out
    asked = _memory._rules_context(str(tmp_path), "create a jira ticket for this bug")
    assert "CLR" in asked


def test_a_user_rule_without_triggers_is_still_always_on(tmp_path, monkeypatch):
    from aiforge_core.runtime import repo_rules
    from aiforge_core.runtime.chat_agent._tools import _memory
    r = repo_rules.Rule(name="vm", globs=(), always=True,
                        body="Run tests on the VM.", source="global")
    monkeypatch.setattr(repo_rules, "load_rules", lambda cwd: [r])
    always, tagged = _memory._repo_rule_bullets(str(tmp_path))
    assert always == ["- Run tests on the VM."] and not tagged


def test_a_user_rule_with_triggers_and_always_apply_stays_always_on(tmp_path, monkeypatch):
    from aiforge_core.runtime import repo_rules
    from aiforge_core.runtime.chat_agent._tools import _memory
    r = repo_rules.Rule(name="vm", globs=(), always=True, triggers=("test",),
                        body="Run tests on the VM.", source="global")
    monkeypatch.setattr(repo_rules, "load_rules", lambda cwd: [r])
    always, tagged = _memory._repo_rule_bullets(str(tmp_path))
    assert always and not tagged


# ── README: words, not badges ──────────────────────────────────────────────
def test_readme_excerpt_drops_badges_and_link_definitions(tmp_path):
    (tmp_path / "README.md").write_text(
        "[pypi]: https://img.shields.io/pypi/v/x.svg\n"
        "[![PyPI][pypi]](https://pypi.org/project/x/)\n"
        "<p align=center><img src=logo.png></p>\n"
        "# x\n\nTOMLKit is a style-preserving TOML library for Python.\n")
    out = _recall._readme_excerpt(str(tmp_path))
    assert "shields" not in out and "pypi.org" not in out and "<p" not in out
    assert "style-preserving TOML library" in out


# ── checklist items are whole items, cut at a word ─────────────────────────
def test_checklist_items_keep_their_continuation_lines():
    asks = _recall._split_asks(SPEC)
    assert len(asks) == 3
    assert "code point up to U+00FF" in asks[2]
    assert "allowed inside the braces" in asks[0]


def test_a_long_item_is_cut_at_a_word_with_an_ellipsis():
    out = _recall._clip("word " * 60, 40)
    assert len(out) <= 40 and out.endswith("…") and not out.endswith(" …")


# ── prompt text ────────────────────────────────────────────────────────────
def test_tool_descriptions_steer_away_from_slices_and_probes():
    NATIVE_TOOL_SCHEMAS = _schemas.NATIVE_TOOL_SCHEMAS
    desc = {s["function"]["name"]: s["function"].get("description", "")
            for s in NATIVE_TOOL_SCHEMAS}
    assert "150+ lines" in desc["read_lines"] and "do not read it again" in desc["read_lines"]
    assert "python -c" in desc["run_command"] and "use the read tools" in desc["run_command"]


def test_prompt_no_longer_asks_for_extra_tests_or_pushes_spawn():
    from aiforge_core.runtime.chat_agent import _native_prompt, _prompt_text
    import inspect
    assert "more tests than the request lists" not in inspect.getsource(_native_prompt)
    assert "do the work yourself by default" in _prompt_text._MULTI_AGENT


def test_no_shipped_rule_rides_on_a_plain_repo_coding_task(tmp_path, monkeypatch):
    """tk-toml11 says "This repository is `tomlkit`": the resolve-by-name
    default (call resolve_repo, do not assume the cwd) fired on "repository"."""
    from aiforge_core.runtime.chat_agent._tools import _memory
    monkeypatch.setattr(_memory, "_md_rule_bullets", lambda cwd: ([], []))
    monkeypatch.setattr(_memory, "_constraint_bullets", lambda cwd: [])
    q = ("This repository is `tomlkit`, the style-preserving TOML parser. "
         "Update the parser in this project so it accepts TOML v1.1.0.")
    assert _memory._rules_context(str(tmp_path), q) == ""
