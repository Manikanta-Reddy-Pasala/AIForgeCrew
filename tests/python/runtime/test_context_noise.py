"""A long coding request pulls in no unrelated playbooks, and format strings
in it are not repositories.

Measured on a 17K-character spec (2026-10-15): the Jira, Confluence and
PR-review playbooks (about 11K characters) were injected with no trigger
word in the request, and ``INV/{financial_year}/{seq:04d}`` made the chat
announce three repositories."""
from __future__ import annotations

from aiforge_core.runtime import repo_roles, skills
from aiforge_core.runtime.skills import Skill

_REVIEW = Skill(name="review-pull-request",
                description="How to review a pull or merge request: fetch the "
                            "diff, check it, run the tests, post findings",
                body="...", triggers=("review pr", "review the pr", "code review"))

_SPEC_WORDS = " ".join(f"word{i}" for i in range(300))
_LONG = ("Extend the library: fetch the orders, check every total, run the "
         "tests, post a report of the findings for each diff. " + _SPEC_WORDS)


def test_a_long_request_does_not_pull_in_a_playbook_by_word_overlap():
    hits = skills.search(_LONG, skills=[_REVIEW], damp_long=True)
    assert not hits or hits[0]["score"] < skills._inject_min()


def test_skill_injection_uses_the_damped_score(monkeypatch):
    monkeypatch.setattr(skills, "load", lambda cwd=None: [_REVIEW])
    assert skills.select(_LONG) == []
    assert [s.name for s in skills.select("check the diff, run the tests and "
                                          "post findings")] == ["review-pull-request"]


def test_a_short_request_still_finds_it_by_overlap():
    hits = skills.search("check the diff, run the tests and post findings",
                         skills=[_REVIEW], damp_long=True)
    assert hits and hits[0]["score"] >= 3.0


def test_a_trigger_in_a_long_request_still_counts_in_full():
    hits = skills.search("please do a code review. " + _LONG, skills=[_REVIEW],
                         damp_long=True)
    assert hits and hits[0]["score"] >= skills._inject_min()


def test_format_strings_are_not_folders(tmp_path):
    said = "Number invoices INV/{financial_year}/{seq:04d} and glob /tmp/*.log"
    assert repo_roles._typed_folders(said) == []


def test_a_new_destination_folder_still_counts(tmp_path):
    said = f"port this to {tmp_path}/new-rs"
    assert repo_roles._typed_folders(said) == [str((tmp_path / "new-rs").resolve())]


def test_a_spec_with_numbered_sections_lists_the_sections():
    from aiforge_core.runtime.chat_agent._context._recall import _split_asks
    spec = ("Please extend the library with the features below.\n\n"
            "Conventions:\n- money means a Decimal\n- dates are dates\n\n"
            "## 1. Batches and expiry\n\n1. New dataclass Batch with fields\n"
            "2. MovementKind gains two members\n\n"
            "## 2. Coupon codes\n\n- add a coupon type\n- validate codes\n\n"
            "## 3. Returns\n\nSome text.\n")
    assert _split_asks(spec) == ["1. Batches and expiry", "2. Coupon codes",
                                 "3. Returns"]


def test_unnumbered_headings_are_not_asks():
    from aiforge_core.runtime.chat_agent._context._recall import _split_asks
    doc = ("## Background\n\nWe sell rice.\n\n## Notes\n\nNone.\n\n"
           "- fix the order total rounding\n- add a test for it\n")
    assert _split_asks(doc) == ["fix the order total rounding", "add a test for it"]


def test_a_long_bulleted_spec_gets_no_truncated_checklist():
    from aiforge_core.runtime.chat_agent._context._recall import _split_asks
    spec = "Build this:\n" + "\n".join(f"- field number {i} is a string" for i in range(30))
    assert _split_asks(spec) == []


def test_rules_and_other_callers_are_not_damped():
    # trigger-gated rules share the scorer: a long message must not fade them
    full = skills.search(_LONG, skills=[_REVIEW])
    damped = skills.search(_LONG, skills=[_REVIEW], damp_long=True)
    assert full and full[0]["score"] > (damped[0]["score"] if damped else 0)


def test_more_numbered_sections_than_the_cap_give_no_checklist():
    from aiforge_core.runtime.chat_agent._context._recall import _split_asks
    spec = "\n".join(f"## {i}. Section number {i}\n\ntext" for i in range(1, 12))
    assert _split_asks(spec) == []


def test_a_year_heading_is_not_a_numbered_section():
    from aiforge_core.runtime.chat_agent._context._recall import _heading_asks
    assert _heading_asks("## 2024: notes\n\n## 2025: plans\n") == []


def test_small_windows_keep_the_old_condense_point(monkeypatch):
    import aiforge_core.runtime.chat_agent._context._window as w
    monkeypatch.delenv("AIFORGE_CTX_HISTORY_FRACTION", raising=False)
    monkeypatch.setattr(w, "_window_tokens", lambda role=None: 32768)
    assert w._history_fraction("chat") == 0.75
    monkeypatch.setattr(w, "_window_tokens", lambda role=None: 131072)
    assert w._history_fraction("chat") == 0.50


def test_a_new_folder_with_a_non_ascii_name_counts(tmp_path):
    said = f"put it in {tmp_path}/données"
    assert repo_roles._typed_folders(said) == [str((tmp_path / "données").resolve())]


def test_headings_inside_a_pasted_script_are_not_asks():
    from aiforge_core.runtime.chat_agent._context._recall import _split_asks
    msg = ("Why does this script fail on the server?\n\n```bash\n"
           "## Step 1 install\napt-get install x\n## Step 2 run\n./run\n```\n")
    assert _split_asks(msg) == []


def test_adk_trims_at_the_doer_windows_fraction(monkeypatch):
    import aiforge_core.runtime.chat_agent._context._window as w
    from aiforge_core.runtime.adk_runner import _context as adk
    monkeypatch.delenv("AIFORGE_CTX_HISTORY_FRACTION", raising=False)
    monkeypatch.setattr(w, "_window_tokens",
                        lambda role=None: 32768 if role == "doer" else 131072)
    assert adk._history_frac() == 0.75
