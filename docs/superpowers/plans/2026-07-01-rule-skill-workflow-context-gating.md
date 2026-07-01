# Rule/Skill/Workflow Context Gating + Disambiguation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate rules/skills/workflows by topic relevance instead of dumping everything every turn, and surface near-tie ambiguity to the user instead of silently auto-picking — mode-dependent (block in simple/plan chat and interactive team tickets; best-guess + notice for autonomous tickets).

**Architecture:** One shared trigger-scorer (`skills.select_or_ask`) extended with a confidence-margin ambiguity check, reused by `workflows.py` and a new trigger-aware path in `repo_rules.py`. Disambiguation reuses existing surfaces only — the chat agent's `ASK:` protocol, `clarify.py`'s pre-pipeline gate, and a new non-blocking ticket-trace notice for autonomous runs. No new endpoints, no new storage backend, no new UI.

**Tech Stack:** Python 3.11+, pytest, existing `aiforge_core.runtime` modules.

**Spec:** [docs/superpowers/specs/2026-07-01-rule-skill-workflow-context-gating-design.md](../specs/2026-07-01-rule-skill-workflow-context-gating-design.md)

**Env flags introduced:** `AIFORGE_AMBIGUITY_MARGIN` (default `0.15`, `0` disables ambiguity detection entirely), `AIFORGE_AMBIGUITY_FLOOR` (default `2.0`, minimum top score before a near-tie counts as real ambiguity).

---

### Task 1: `skills.py` — shared ambiguity-aware scorer

**Files:**
- Modify: `aiforge_core/runtime/skills.py`
- Test: `tests/python/runtime/test_skills_select_or_ask.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/python/runtime/test_skills_select_or_ask.py`:

```python
"""Tests for skills.select_or_ask — the shared ambiguity-aware scorer used
by skills, workflows, and (via an adapter) repo rules."""
from __future__ import annotations

from aiforge_core.runtime import skills as sk


def _skill(name, triggers, priority=0, always=False):
    return sk.Skill(name=name, description="", triggers=tuple(triggers),
                    body=f"body for {name}", source="", always=always,
                    priority=priority)


def test_select_or_ask_clear_winner_no_ambiguity(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    pool = [_skill("deploy-staging", ["deploy", "staging"]),
            _skill("unrelated", ["billing"])]
    chosen, ambiguous = sk.select_or_ask("deploy staging now", pool=pool)
    assert [s.name for s in chosen] == ["deploy-staging"]
    assert ambiguous == []


def test_select_or_ask_near_tie_is_ambiguous(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_FLOOR", "2.0")
    pool = [_skill("deploy-staging", ["deploy", "staging", "release"]),
            _skill("deploy-prod", ["deploy", "prod", "release"])]
    chosen, ambiguous = sk.select_or_ask("deploy release now", pool=pool)
    assert len(ambiguous) == 1
    assert {s.name for s in ambiguous[0]} == {"deploy-staging", "deploy-prod"}
    # A best-guess is still picked (never silently drops a usable rule).
    assert len(chosen) == 1
    assert chosen[0].name in {"deploy-staging", "deploy-prod"}


def test_select_or_ask_tie_break_by_priority(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_FLOOR", "2.0")
    pool = [_skill("deploy-staging", ["deploy", "staging", "release"], priority=1),
            _skill("deploy-prod", ["deploy", "prod", "release"], priority=5)]
    chosen, ambiguous = sk.select_or_ask("deploy release now", pool=pool)
    assert len(ambiguous) == 1
    assert chosen[0].name == "deploy-prod"   # higher priority wins the tie-break


def test_select_or_ask_always_on_bypasses_ambiguity(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    pool = [_skill("deploy-staging", ["deploy", "staging", "release"], always=True),
            _skill("deploy-prod", ["deploy", "prod", "release"])]
    chosen, ambiguous = sk.select_or_ask("deploy release now", pool=pool)
    assert "deploy-staging" in {s.name for s in chosen}   # always-on, unconditional
    assert ambiguous == []                                 # only one scored candidate


def test_select_or_ask_noise_floor_prevents_false_tie(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_FLOOR", "2.0")
    # Both candidates only weakly overlap ("the") — scores near-zero, below
    # the floor, so this must NOT be reported as ambiguous.
    pool = [_skill("alpha", ["xylophone"]), _skill("beta", ["quokka"])]
    chosen, ambiguous = sk.select_or_ask("the the the", pool=pool)
    assert ambiguous == []


def test_select_or_ask_margin_zero_disables_ambiguity(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0")
    pool = [_skill("deploy-staging", ["deploy", "staging", "release"]),
            _skill("deploy-prod", ["deploy", "prod", "release"])]
    chosen, ambiguous = sk.select_or_ask("deploy release now", pool=pool)
    assert ambiguous == []                          # off switch — old silent-pick behavior
    assert len(chosen) >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/python/runtime/test_skills_select_or_ask.py -v`
Expected: FAIL with `AttributeError: module 'aiforge_core.runtime.skills' has no attribute 'select_or_ask'`

- [ ] **Step 3: Implement `select_or_ask` in `skills.py`**

Add after `select()` (after line 218, before `selected_names`):

```python
def _ambiguity_margin() -> float:
    """Fractional gap under which candidate[1] is considered a near-tie
    with candidate[0] (0.15 = within 15% of the top score). 0 disables
    ambiguity detection entirely (old silent-pick behavior).
    Tunable via AIFORGE_AMBIGUITY_MARGIN (default 0.15)."""
    try:
        return max(0.0, float(os.environ.get("AIFORGE_AMBIGUITY_MARGIN", "0.15")))
    except (TypeError, ValueError):
        return 0.15


def _ambiguity_floor() -> float:
    """Minimum top score before a near-tie counts as real ambiguity — stops
    two near-zero garbage matches from falsely tying.
    Tunable via AIFORGE_AMBIGUITY_FLOOR (default 2.0)."""
    try:
        return max(0.0, float(os.environ.get("AIFORGE_AMBIGUITY_FLOOR", "2.0")))
    except (TypeError, ValueError):
        return 2.0


def select_or_ask(query: str, cwd: str | None = None, k: int = 4,
                  pool: list[Skill] | None = None,
                  ) -> tuple[list[Skill], list[list[Skill]]]:
    """Like :func:`select` but separates out AMBIGUOUS near-ties instead of
    silently auto-picking one. Returns ``(chosen, ambiguous_groups)``:

    - ``chosen`` — always-on items + top relevant matches, same as
      :func:`select`. When a near-tie is detected, ONE best-guess (highest
      priority, ties broken by score) from that tie is still included here
      — a caller that can't block (an autonomous run) still gets a usable
      pick.
    - ``ambiguous_groups`` — each entry is a list of 2+ Skills that scored
      within :func:`_ambiguity_margin` of each other and needed a
      best-guess instead of a confident pick. A caller that CAN ask a user
      (a live chat turn, an interactive ticket) surfaces this for
      disambiguation.

    ``always``-on items always bypass this — they are never ambiguous."""
    src_pool = pool if pool is not None else load(cwd)
    always_cap = int(os.environ.get("AIFORGE_SKILLS_ALWAYS_CAP", "8"))
    always_on = sorted((s for s in src_pool if s.always),
                       key=lambda s: -s.priority)[:always_cap]
    always_names = {s.name for s in always_on}
    chosen: dict[str, Skill] = {s.name: s for s in always_on}
    ambiguous: list[list[Skill]] = []
    margin = _ambiguity_margin()
    floor = _ambiguity_floor()
    # Always-on items are unconditionally included regardless of score —
    # exclude them from consideration entirely, otherwise a high-scoring
    # always-on skill can falsely "tie" with an unrelated match (there's
    # nothing to disambiguate: the always-on one applies no matter what).
    # The floor filter applies to EVERY candidate, not just the ambiguity
    # check — a weak, barely-nonzero token-overlap match (e.g. sharing only
    # a common word like "to") must not leak into the final selection just
    # because the pool happens to be small enough that top-k includes it.
    hits = [h for h in search(query, cwd, k=max(k, 4), skills=src_pool)
           if h["name"] not in always_names and h["score"] >= floor]
    if not hits:
        return sorted(chosen.values(), key=lambda s: -s.priority), ambiguous
    top_score = hits[0]["score"]
    if (margin > 0 and len(hits) > 1
            and hits[1]["score"] >= top_score * (1 - margin)):
        near = [h for h in hits if h["score"] >= top_score * (1 - margin)]
        near_names = {h["name"] for h in near}
        group = [s for s in src_pool if s.name in near_names]
        if len(group) > 1:
            ambiguous.append(group)
            score_by_name = {h["name"]: h["score"] for h in near}
            best = sorted(
                group, key=lambda s: (-s.priority, -score_by_name[s.name]))[0]
            chosen[best.name] = best
            hits = hits[len(near):]   # remaining non-ambiguous hits below
    for h in hits[:k]:
        sk_hit = next((s for s in src_pool if s.name == h["name"]), None)
        if sk_hit is not None:
            chosen[sk_hit.name] = sk_hit
    return sorted(chosen.values(), key=lambda s: -s.priority), ambiguous
```

Also add `"select_or_ask"` to the `__all__` list at the bottom of the file.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/python/runtime/test_skills_select_or_ask.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Run the full existing skills-related test suite to check no regression**

Run: `python3 -m pytest tests/python/test_enhancer_architect_context.py -q`
Expected: PASS (16 passed, unchanged — `select_or_ask` is additive, `select`/`search`/`auto_context` untouched)

- [ ] **Step 6: Commit**

```bash
git add aiforge_core/runtime/skills.py tests/python/runtime/test_skills_select_or_ask.py
git commit -m "feat: add skills.select_or_ask — shared ambiguity-aware scorer"
```

---

### Task 2: `workflows.py` — reuse the same scorer

**Files:**
- Modify: `aiforge_core/runtime/workflows.py`
- Test: `tests/python/runtime/test_workflows_select_or_ask.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/python/runtime/test_workflows_select_or_ask.py`:

```python
"""workflows.select_or_ask must reuse skills.select_or_ask's scorer/
ambiguity logic against the workflow pool — same mechanism, different
folder, per repo_rules.py-adjacent design decision to unify skills/
workflows/rules disambiguation on one scorer."""
from __future__ import annotations

from aiforge_core.runtime import workflows as wf
from aiforge_core.runtime.skills import Skill


def test_workflows_select_or_ask_reuses_skills_scorer(monkeypatch):
    pool = [
        Skill(name="ship-staging", description="", triggers=("deploy", "staging"),
             body="staging steps", source="", always=False, priority=1),
        Skill(name="ship-prod", description="", triggers=("deploy", "prod"),
             body="prod steps", source="", always=False, priority=1),
    ]
    monkeypatch.setattr(wf, "load", lambda cwd=None: pool)
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_FLOOR", "2.0")
    chosen, ambiguous = wf.select_or_ask("deploy this now")
    assert len(ambiguous) == 1
    assert {s.name for s in ambiguous[0]} == {"ship-staging", "ship-prod"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/python/runtime/test_workflows_select_or_ask.py -v`
Expected: FAIL with `AttributeError: module 'aiforge_core.runtime.workflows' has no attribute 'select_or_ask'`

- [ ] **Step 3: Implement `select_or_ask` in `workflows.py`**

Add after `select()` (after line 97, before `selected_names`):

```python
def select_or_ask(query: str, cwd: str | None = None, k: int = 3,
                  ) -> tuple[list[Skill], list[list[Skill]]]:
    """Like :func:`select` but returns ambiguous near-ties separately
    instead of silently auto-picking (same scorer as skills.select_or_ask)."""
    return _sk.select_or_ask(query, cwd, k=k, pool=load(cwd))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/python/runtime/test_workflows_select_or_ask.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/workflows.py tests/python/runtime/test_workflows_select_or_ask.py
git commit -m "feat: add workflows.select_or_ask reusing skills scorer"
```

---

### Task 3: `repo_rules.py` — optional `triggers:` frontmatter + trigger-scored matching

**Files:**
- Modify: `aiforge_core/runtime/repo_rules.py`
- Test: `tests/python/runtime/test_repo_rules_triggers.py` (new)

`match_rules()`/`collect()`/`matched_names()` stay glob-only and UNCHANGED (other callers — `graph_pipeline.py:358`, `parallel_subtasks.py:871` — keep working exactly as today, no migration needed). This task is purely additive: a new `triggers` field on `Rule`, and new `match_rules_with_triggers()` / `collect_or_ask()` functions that OR the existing glob match with the new trigger score.

- [ ] **Step 1: Write the failing tests**

Create `tests/python/runtime/test_repo_rules_triggers.py`:

```python
"""Trigger-scored rule matching — additive to the existing glob-only
match_rules()/collect(). A rule with globs that don't hit the ticket's
scope can still apply via a trigger-score match against the ticket text."""
from __future__ import annotations

from aiforge_core.runtime import repo_rules as rr


def _rule(name, triggers=(), globs=(), always=False, body="do the thing"):
    return rr.Rule(name=name, globs=tuple(globs), always=always, body=body,
                   source=f"{name}.md", triggers=tuple(triggers))


def test_rule_triggers_field_defaults_empty():
    r = rr.Rule(name="x", globs=(), always=False, body="b", source="s")
    assert r.triggers == ()


def test_parse_rule_file_reads_triggers_frontmatter(tmp_path):
    p = tmp_path / "deploy.md"
    p.write_text(
        "---\nname: deploy-staging\ntriggers: [deploy, staging]\n---\n\n"
        "Always tag staging builds with the branch name.\n")
    r = rr._parse_rule_file(p)
    assert r is not None
    assert r.triggers == ("deploy", "staging")
    assert r.always is False


def test_match_rules_with_triggers_matches_by_query(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    rules = [_rule("deploy-staging", triggers=["deploy", "staging"])]
    matched, ambiguous = rr.match_rules_with_triggers(
        rules, scope_globs=[], query="please deploy to staging")
    assert [r.name for r in matched] == ["deploy-staging"]
    assert ambiguous == []


def test_match_rules_with_triggers_glob_still_works(monkeypatch):
    rules = [_rule("py-style", globs=["**/*.py"])]
    matched, ambiguous = rr.match_rules_with_triggers(
        rules, scope_globs=["src/a/**"], query="unrelated text")
    assert [r.name for r in matched] == ["py-style"]   # glob path unaffected


def test_match_rules_with_triggers_ambiguous_pair(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_FLOOR", "2.0")
    rules = [_rule("deploy-staging", triggers=["deploy", "staging", "release"]),
             _rule("deploy-prod", triggers=["deploy", "prod", "release"])]
    matched, ambiguous = rr.match_rules_with_triggers(
        rules, scope_globs=[], query="deploy release now")
    assert len(ambiguous) == 1
    assert {r.name for r in ambiguous[0]} == {"deploy-staging", "deploy-prod"}
    assert len(matched) == 1   # best-guess still included


def test_collect_or_ask_renders_and_reports_ambiguous(monkeypatch, tmp_path):
    (tmp_path / ".aiforge" / "rules").mkdir(parents=True)
    (tmp_path / ".aiforge" / "rules" / "a.md").write_text(
        "---\nname: deploy-staging\ntriggers: [deploy, staging, release]\n---\n\nStep A\n")
    (tmp_path / ".aiforge" / "rules" / "b.md").write_text(
        "---\nname: deploy-prod\ntriggers: [deploy, prod, release]\n---\n\nStep B\n")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_FLOOR", "2.0")
    monkeypatch.setattr(rr, "load_global_rules", lambda: [])
    rendered, ambiguous = rr.collect_or_ask(str(tmp_path), [], "deploy release now")
    assert rendered   # best-guess rendered
    assert len(ambiguous) == 1


def test_collect_or_ask_soft_fails_to_empty(monkeypatch):
    rendered, ambiguous = rr.collect_or_ask("/no/such/dir", [], "anything")
    assert rendered == ""
    assert ambiguous == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/python/runtime/test_repo_rules_triggers.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'triggers'` (Rule has no `triggers` field yet) and `AttributeError` for the two missing functions.

- [ ] **Step 3: Implement in `repo_rules.py`**

Modify the `Rule` dataclass (around line 46-53):

```python
@dataclass(frozen=True)
class Rule:
    name: str
    globs: tuple[str, ...]      # empty = always applies
    always: bool
    body: str
    source: str
    triggers: tuple[str, ...] = ()   # NEW — optional topic gate (OR'd with globs)
```

Modify `_parse_rule_file` (around line 55-81) to read the new field — insert before the `return Rule(...)`:

```python
    raw_triggers = meta.get("triggers") or []
    if isinstance(raw_triggers, str):
        raw_triggers = [t.strip() for t in raw_triggers.split(",")]
    triggers = tuple(str(t).lower() for t in raw_triggers
                     if isinstance(t, str) and t.strip())
```

And update the `return` to add `triggers=triggers,`.

Add after `match_rules()` (after line 210):

```python
def match_rules_with_triggers(
    rules: list[Rule], scope_globs: list[str] | None, query: str,
) -> tuple[list[Rule], list[list[Rule]]]:
    """Like :func:`match_rules` but ALSO scores rules with no glob hit
    against ``query`` (the ticket title+body) via the same trigger-scorer
    skills/workflows use. Returns ``(matched, ambiguous_groups)`` — a rule
    fires if its globs match (as before) OR its triggers score a confident
    hit against ``query``; a near-tie among trigger-scored candidates is
    reported in ``ambiguous_groups`` instead of silently picked (a
    best-guess is still included in ``matched``, same contract as
    :func:`aiforge_core.runtime.skills.select_or_ask`)."""
    matched: list[Rule] = []
    trigger_pool: list[Rule] = []
    for r in rules:
        # "no globs" only means always-applies when there are ALSO no
        # triggers — a trigger-only rule (globs=() but triggers set) must
        # still go through the trigger scorer below, not be treated as
        # unconditional (that would silently bypass gating entirely).
        if r.always or (not r.globs and not r.triggers):
            matched.append(r)
            continue
        glob_hit = bool(r.globs) and any(
            _globs_intersect(rg, sg) for rg in r.globs
            for sg in (scope_globs or []))
        if glob_hit:
            matched.append(r)
        elif r.triggers:
            trigger_pool.append(r)
    ambiguous: list[list[Rule]] = []
    if query and trigger_pool:
        from aiforge_core.runtime import skills as _sk
        pool_skills = [
            _sk.Skill(name=r.name, description="", triggers=r.triggers,
                      body=r.body, source=r.source, always=False, priority=0)
            for r in trigger_pool]
        chosen_sk, amb_sk = _sk.select_or_ask(
            query, k=len(trigger_pool), pool=pool_skills)
        by_name = {r.name: r for r in trigger_pool}
        matched.extend(by_name[s.name] for s in chosen_sk if s.name in by_name)
        for grp in amb_sk:
            ambiguous.append([by_name[s.name] for s in grp if s.name in by_name])
    return matched, ambiguous


def collect_or_ask(repo_root: str | Path, scope_globs: list[str] | None,
                   query: str) -> tuple[str, list[list[Rule]]]:
    """Like :func:`collect` but trigger-aware — rules with no glob hit are
    also scored against ``query``. Returns ``(rendered_md, ambiguous_groups)``.
    '' + [] on any error (rules must never block a run)."""
    try:
        rules = load_rules(repo_root)
        if not rules:
            return "", []
        matched, ambiguous = match_rules_with_triggers(rules, scope_globs, query)
        return render(matched), ambiguous
    except Exception as exc:  # noqa: BLE001
        log.debug("repo_rules.collect_or_ask failed: %s", exc)
        return "", []
```

Update `__all__` at the bottom to include `"match_rules_with_triggers"` and `"collect_or_ask"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/python/runtime/test_repo_rules_triggers.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Run the full existing repo_rules-dependent suite to check no regression**

Run: `python3 -m pytest tests/python/test_enhancer_architect_context.py -q`
Expected: PASS (16 passed — `collect`/`match_rules`/`matched_names` untouched)

- [ ] **Step 6: Commit**

```bash
git add aiforge_core/runtime/repo_rules.py tests/python/runtime/test_repo_rules_triggers.py
git commit -m "feat: add repo_rules.collect_or_ask — trigger-scored rule matching"
```

---

### Task 4: `chat_agent.py` — gate `_rules_context()` by topic, inject ambiguity note

**Files:**
- Modify: `aiforge_core/runtime/chat_agent.py:508-526` (`_rules_context`), `:1906` (call site)
- Test: `tests/python/test_chat_agent_rules_context.py` (new)

Bullets get an OPTIONAL inline tag convention: `- [triggers: deploy, staging] Always tag staging builds with the branch name`. Untagged bullets (every rule captured before this change) stay always-on — no migration, no data rewrite.

- [ ] **Step 1: Write the failing tests**

Create `tests/python/test_chat_agent_rules_context.py`:

```python
"""_rules_context gates tagged rule bullets by topic relevance; untagged
(legacy) bullets stay always-on for backward compatibility."""
from __future__ import annotations

from aiforge_core.runtime import chat_agent as ca


def _fake_doc(body: str):
    return {"body": body}


def test_untagged_bullets_always_included(monkeypatch, tmp_path):
    monkeypatch.setattr(ca, "_repo_name", lambda cwd: "demo")

    class _FakePath:
        pass

    def fake_find_by_source(src):
        if src == "rules:global":
            return "PATH"
        return None

    def fake_parse(p):
        return _fake_doc("- always use yarn, not npm")

    monkeypatch.setattr("aiforge_core.memory.md_store._find_by_source",
                        fake_find_by_source)
    monkeypatch.setattr("aiforge_core.memory.md_store._parse", fake_parse)
    out = ca._rules_context("/repo", "totally unrelated query")
    assert "always use yarn" in out


def test_tagged_bullet_gated_by_query(monkeypatch):
    monkeypatch.setattr(ca, "_repo_name", lambda cwd: "demo")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")

    def fake_find_by_source(src):
        return "PATH" if src == "rules:global" else None

    def fake_parse(p):
        return _fake_doc(
            "- [triggers: deploy, staging] tag staging builds with branch\n"
            "- [triggers: billing, invoice] always round invoices to 2dp")

    monkeypatch.setattr("aiforge_core.memory.md_store._find_by_source",
                        fake_find_by_source)
    monkeypatch.setattr("aiforge_core.memory.md_store._parse", fake_parse)

    out = ca._rules_context("/repo", "please deploy to staging now")
    assert "tag staging builds" in out
    assert "round invoices" not in out


def test_ambiguous_tagged_bullets_inject_ask_note(monkeypatch):
    monkeypatch.setattr(ca, "_repo_name", lambda cwd: "demo")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_FLOOR", "2.0")

    def fake_find_by_source(src):
        return "PATH" if src == "rules:global" else None

    def fake_parse(p):
        return _fake_doc(
            "- [triggers: deploy, staging, release] ship to staging first\n"
            "- [triggers: deploy, prod, release] ship to prod first")

    monkeypatch.setattr("aiforge_core.memory.md_store._find_by_source",
                        fake_find_by_source)
    monkeypatch.setattr("aiforge_core.memory.md_store._parse", fake_parse)

    out = ca._rules_context("/repo", "deploy release now")
    assert "AMBIGUOUS RULE MATCH" in out
    assert "ASK" in out


def test_no_rules_returns_empty(monkeypatch):
    monkeypatch.setattr(ca, "_repo_name", lambda cwd: "demo")
    monkeypatch.setattr("aiforge_core.memory.md_store._find_by_source",
                        lambda src: None)
    assert ca._rules_context("/repo", "anything") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/python/test_chat_agent_rules_context.py -v`
Expected: FAIL — `TypeError: _rules_context() takes 1 positional argument but 2 were given`

- [ ] **Step 3: Implement in `chat_agent.py`**

Replace `_rules_context` (lines 508-526) with:

`chat_agent.py` already imports `re` at module scope (line 31) — no new import needed.

```python
_BULLET_TRIGGERS_RE = re.compile(r"^\[triggers:\s*([^\]]*)\]\s*(.*)$")


def _parse_bullet(line: str) -> tuple[tuple[str, ...], str]:
    """Strip a leading '- ' and an optional '[triggers: a, b]' prefix.
    Returns (triggers, text). No triggers prefix → triggers=() (always-on,
    backward compatible with every bullet written before this feature)."""
    text = line[2:] if line.startswith("- ") else line
    m = _BULLET_TRIGGERS_RE.match(text.strip())
    if not m:
        return (), text.strip()
    trig = tuple(t.strip().lower() for t in m.group(1).split(",") if t.strip())
    return trig, m.group(2).strip()


def _rules_context(cwd: str, query: str = "") -> str:
    """The user's persistent rule book (global + this-repo), injected into
    EVERY session so the rules are always honoured. Untagged bullets are
    always-on (legacy). Bullets tagged with an inline '[triggers: ...]'
    prefix are gated by relevance to ``query`` via the shared scorer; a
    near-tie among tagged bullets injects an ASK note instead of silently
    picking one."""
    try:
        from aiforge_core.memory import md_store
        from aiforge_core.runtime import skills as _sk
        always_lines: list[str] = []
        tagged: list[_sk.Skill] = []
        for src in ("rules:global", f"rules:{_repo_name(cwd)}"):
            p = md_store._find_by_source(src)
            if p is None:
                continue
            body = md_store._parse(p).get("body", "")
            for line in body.splitlines():
                if not line.strip():
                    continue
                trig, text = _parse_bullet(line)
                if not trig:
                    always_lines.append("- " + text)
                else:
                    tagged.append(_sk.Skill(
                        name=text[:60], description="", triggers=trig,
                        body=text, source=src, always=False, priority=0))
        blocks: list[str] = list(always_lines)
        ambiguous_note = ""
        if tagged:
            if query:
                chosen, ambiguous = _sk.select_or_ask(
                    query, pool=tagged, k=len(tagged))
                blocks.extend("- " + s.body for s in chosen)
                if ambiguous:
                    names = " or ".join(f"'{s.body}'" for s in ambiguous[0])
                    ambiguous_note = (
                        "\nAMBIGUOUS RULE MATCH: " + names + " both matched "
                        "— ASK the user which applies before proceeding, "
                        "don't guess.")
            else:
                # No query to score against (defensive) — fail open.
                blocks.extend("- " + s.body for s in tagged)
        if not blocks:
            return ""
        return ("RULES — the user told you to ALWAYS follow these, every "
                "session (HIGHEST priority, override defaults):\n"
                + "\n".join(blocks)[:1800] + ambiguous_note)
    except Exception:  # noqa: BLE001
        return ""
```

Update the call site at line 1906 from:

```python
    rules = _rules_context(cwd)
```

to:

```python
    rules = _rules_context(cwd, last_user)
```

(`last_user` is already computed above this line at 1894-1899.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/python/test_chat_agent_rules_context.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Run the broader chat_agent test suite to check no regression**

Run: `python3 -m pytest tests/python/ -k chat_agent -q`
Expected: PASS, no new failures beyond any pre-existing unrelated ones

- [ ] **Step 6: Commit**

```bash
git add aiforge_core/runtime/chat_agent.py tests/python/test_chat_agent_rules_context.py
git commit -m "feat: gate simple/plan chat rules by topic, ask on ambiguous match"
```

---

### Task 5: `rule_capture.py` — capture optional triggers at classify time

**Files:**
- Modify: `aiforge_core/runtime/rule_capture.py`
- Test: `tests/python/test_rule_capture_triggers.py` (new)

Per the approved rule-capture design, rules are captured via ONE LLM classify call. Extend its output schema with an optional `triggers` list (the classifier already reads the message; inferring 1-3 topic words is a natural extension of `canonical`/`scope`, not a new LLM call). Threaded into BOTH storage paths `_do_store` already writes: the md_store bullet (as the inline `[triggers: ...]` tag Task 4 parses) and the `.aiforge/rules/<slug>.md` file (as real frontmatter Task 3 parses).

- [ ] **Step 1: Write the failing tests**

Create `tests/python/test_rule_capture_triggers.py`:

```python
"""rule_capture threads an optional `triggers` field from classify() through
both storage paths: the md_store bullet (inline tag) and the .aiforge/rules
file (frontmatter)."""
from __future__ import annotations

from aiforge_core.runtime import rule_capture as rc


def test_parse_classification_reads_triggers():
    raw = ('{"category":"rule","scope":"project",'
          '"canonical":"tag staging builds with branch",'
          '"confidence":0.9,"task_present":false,'
          '"triggers":["deploy","staging"]}')
    c = rc._parse_classification(raw)
    assert c["triggers"] == ["deploy", "staging"]


def test_parse_classification_defaults_triggers_empty():
    raw = ('{"category":"rule","scope":"global","canonical":"always use yarn",'
          '"confidence":0.9,"task_present":false}')
    c = rc._parse_classification(raw)
    assert c["triggers"] == []


def test_write_repo_rule_embeds_triggers(tmp_path):
    path = rc._write_repo_rule(str(tmp_path), "deploy-staging",
                               "tag staging builds with branch",
                               triggers=["deploy", "staging"])
    text = open(path, encoding="utf-8").read()
    assert "triggers: [deploy, staging]" in text
    assert "alwaysApply: true" not in text


def test_write_repo_rule_no_triggers_stays_always(tmp_path):
    path = rc._write_repo_rule(str(tmp_path), "always-yarn", "always use yarn")
    text = open(path, encoding="utf-8").read()
    assert "alwaysApply: true" in text


def test_do_store_tags_md_bullet_with_triggers(monkeypatch, tmp_path):
    calls = {}

    def fake_append_bullet(*, source, title, bullet, kind, tags):
        calls["bullet"] = bullet

    monkeypatch.setattr("aiforge_core.memory.md_store.append_bullet",
                        fake_append_bullet)
    monkeypatch.setattr(rc, "_write_repo_rule", lambda *a, **k: None)
    c = {"category": "rule", "scope": "global",
        "canonical": "tag staging builds with branch",
        "confidence": 0.9, "task_present": False,
        "triggers": ["deploy", "staging"]}
    rc._do_store(c, rid="abc", repo="demo", session_id=None, repo_root=None)
    assert calls["bullet"] == "[triggers: deploy, staging] tag staging builds with branch"


def test_do_store_no_triggers_bullet_untagged(monkeypatch):
    calls = {}

    def fake_append_bullet(*, source, title, bullet, kind, tags):
        calls["bullet"] = bullet

    monkeypatch.setattr("aiforge_core.memory.md_store.append_bullet",
                        fake_append_bullet)
    c = {"category": "rule", "scope": "global", "canonical": "always use yarn",
        "confidence": 0.9, "task_present": False, "triggers": []}
    rc._do_store(c, rid="abc", repo="demo", session_id=None, repo_root=None)
    assert calls["bullet"] == "always use yarn"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/python/test_rule_capture_triggers.py -v`
Expected: FAIL — `KeyError: 'triggers'` and `TypeError: _write_repo_rule() got an unexpected keyword argument 'triggers'`

- [ ] **Step 3: Implement in `rule_capture.py`**

Update `_SYS` (lines 141-167) — add a `triggers` instruction and schema field:

```python
_SYS = (
    "You are a STRICT classifier that detects whether a user's chat message "
    "carries something the assistant should REMEMBER and apply later.\n\n"
    "Classify into exactly one category:\n"
    "- \"rule\": a standing directive / instruction about how to behave "
    "(\"always use yarn\", \"commit directly, the machine has access\", "
    "\"never force-push\").\n"
    "- \"memory\": a durable fact/preference to recall later "
    "(\"the staging DB is at db.staging\", \"my name is Sam\").\n"
    "- \"feedback\": a correction/preference on prior behaviour, softer than a "
    "hard rule (\"that was too verbose\", \"prefer shorter commits\").\n"
    "- \"none\": an ordinary task/question with nothing to remember.\n\n"
    "Also choose a scope:\n"
    "- \"global\": applies everywhere, all repos/sessions.\n"
    "- \"project\": applies to THIS repo only.\n"
    "- \"session\": applies to THIS conversation only.\n\n"
    "Default to \"project\" when the user references this repo/folder, "
    "\"global\" for universal directives, \"session\" for a one-off.\n\n"
    "Set \"task_present\" true when the message ALSO asks you to DO something "
    "now (build/fix/run/answer) in addition to stating the rule; false when it "
    "is PURELY a rule/fact/correction with no action requested.\n\n"
    "For \"rule\" or \"feedback\" ONLY: if the rule is scoped to a specific "
    "topic (e.g. deploys, a specific tool, a specific kind of file) rather "
    "than a universal directive, set \"triggers\" to 1-3 short lowercase "
    "topic words; leave it an empty list [] when the rule should ALWAYS "
    "apply regardless of topic.\n\n"
    "Respond with STRICT JSON ONLY, no prose, no code fence:\n"
    '{\"category\":\"rule|memory|feedback|none\",'
    '\"scope\":\"global|project|session\",'
    '\"canonical\":\"<cleaned one-line directive/fact>\",'
    '\"confidence\":0.0-1.0,\"task_present\":true|false,'
    '\"triggers\":[\"...\"]}'
)
```

Update `_parse_classification` (lines 210-229) to extract `triggers` — insert before the final `return`:

```python
    triggers_raw = obj.get("triggers") or []
    if not isinstance(triggers_raw, list):
        triggers_raw = []
    triggers = [str(t).strip().lower() for t in triggers_raw
               if isinstance(t, str) and t.strip()][:3]
```

and add `"triggers": triggers,` to the returned dict.

Update `_write_repo_rule` (lines 316-332) to accept and embed triggers:

```python
def _write_repo_rule(repo_root: str, name: str, body: str,
                     triggers: list[str] | None = None) -> str | None:
    """Best-effort write of a Cursor-style rule into ``<repo_root>/.aiforge/
    rules/<slug>.md`` so the ticket/doer repo_rules pipeline honors it too.
    ``triggers`` (if any) makes the rule topic-gated instead of always-on.
    Returns the path written, or None on any failure."""
    try:
        d = Path(repo_root).expanduser() / ".aiforge" / "rules"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_slug(name)}.md"
        trig = [t.strip().lower() for t in (triggers or []) if str(t).strip()]
        front = "---\n" + f"name: {name}\n"
        if trig:
            front += "triggers: [" + ", ".join(trig) + "]\n"
        else:
            front += "alwaysApply: true\n"
        front += "---\n\n"
        path.write_text(front + body.strip() + "\n", encoding="utf-8")
        return str(path)
    except Exception as exc:  # noqa: BLE001
        log.debug("rule_capture repo-rule write failed: %s", exc)
        return None
```

Update `_do_store` (lines 342-393) to thread `triggers` through both paths — in the `if cat in ("rule", "feedback"):` branch, replace the bullet-building + `_write_repo_rule` call:

```python
        if cat in ("rule", "feedback"):
            triggers = c.get("triggers") or []
            item["triggers"] = triggers
            if scope == "global":
                src, title = "rules:global", "AIForge rules (all sessions)"
            else:  # project
                r = repo or "project"
                src, title = f"rules:{r}", f"{r} — rules"
            bullet_text = (
                f"[triggers: {', '.join(triggers)}] {canonical}"
                if triggers else canonical)
            md_store.append_bullet(source=src, title=title, bullet=bullet_text,
                                   kind=cat, tags=[cat, scope])
            item["md_source"] = src
            item["md_bullet"] = "- " + bullet_text
            item["location"] = f"md:{src}"
            if scope == "project" and repo_root:
                rp = _write_repo_rule(repo_root, canonical[:60] or "rule",
                                      canonical, triggers=triggers)
                if rp:
                    item["rule_path"] = rp
```

Also add `"triggers": []` as a default key in the `item` dict literal near the top of `_do_store` (around line 347-356), so items where `cat` isn't `rule`/`feedback` still carry a consistent shape:

```python
    item = {
        "id": rid, "category": cat, "scope": scope, "canonical": canonical,
        "repo": repo, "session_id": (str(session_id) if session_id is not None else None),
        "location": "", "md_source": None, "md_bullet": None,
        "rule_path": None, "undone": False, "triggers": [],
        "applied_flags": [],
    }
```

(The `item["triggers"] = triggers` line inside the `rule`/`feedback` branch above then overwrites this default with the real value when applicable.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/python/test_rule_capture_triggers.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Run the existing rule_capture suite to check no regression**

Run: `python3 -m pytest tests/api/test_rule_capture_api.py -q`
Expected: PASS, unchanged (`triggers` is additive with an empty-list default everywhere)

- [ ] **Step 6: Commit**

```bash
git add aiforge_core/runtime/rule_capture.py tests/python/test_rule_capture_triggers.py
git commit -m "feat: rule_capture threads optional triggers into bullet + repo-rule storage"
```

---

### Task 6: `clarify.py` — surface ambiguous rule matches to the interactive-ticket gate

**Files:**
- Modify: `aiforge_core/runtime/clarify.py`
- Test: `tests/python/test_clarify_ambiguous.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/python/test_clarify_ambiguous.py`:

```python
"""clarify.py's pre-pipeline gate surfaces ambiguous rule matches as extra
signal for the LLM clarity check — self-contained, computes its own
ambiguity check so it works BEFORE the pipeline's own rules collection."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.runtime import clarify as cl


def _ticket(interactive=True, clarified=False, metadata=None):
    md = dict(metadata or {})
    md["interactive"] = interactive
    md["clarified"] = clarified
    return SimpleNamespace(id=1, identifier="T-1", title="Deploy",
                           body="deploy release now", metadata=md)


def test_ambiguous_candidates_empty_on_no_rules(monkeypatch):
    monkeypatch.setattr(
        "aiforge_core.runtime.repo_rules.collect_or_ask",
        lambda *a, **k: ("", []))
    assert cl._ambiguous_candidates(_ticket()) == []


def test_ambiguous_candidates_reports_names(monkeypatch):
    from aiforge_core.runtime.repo_rules import Rule
    r1 = Rule(name="deploy-staging", globs=(), always=False, body="b",
              source="s", triggers=("deploy",))
    r2 = Rule(name="deploy-prod", globs=(), always=False, body="b",
              source="s", triggers=("deploy",))
    monkeypatch.setattr(
        "aiforge_core.runtime.repo_rules.collect_or_ask",
        lambda *a, **k: ("rendered", [[r1, r2]]))
    names = cl._ambiguous_candidates(_ticket())
    assert names == ["'deploy-staging' or 'deploy-prod'"]


def test_ask_llm_includes_ambiguous_note(monkeypatch):
    seen = {}

    def fake_complete(role, convo, **kw):
        seen["user"] = convo[-1]["content"]
        return "CLEAR"

    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)
    cl._ask_llm(_ticket(), ambiguous=["'deploy-staging' or 'deploy-prod'"])
    assert "deploy-staging" in seen["user"]
    assert "near-equal confidence" in seen["user"]


def test_maybe_clarify_still_skips_autonomous_tickets(monkeypatch):
    def must_not_run(*a, **k):
        raise AssertionError("clarify LLM must not run for autonomous tickets")

    monkeypatch.setattr(cl, "_ask_llm", must_not_run)
    assert cl.maybe_clarify(_ticket(interactive=False)) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/python/test_clarify_ambiguous.py -v`
Expected: FAIL — `AttributeError: module 'aiforge_core.runtime.clarify' has no attribute '_ambiguous_candidates'`, and `_ask_llm() got an unexpected keyword argument 'ambiguous'`

- [ ] **Step 3: Implement in `clarify.py`**

Add after `_clarified` (after line 36):

```python
def _ambiguous_candidates(ticket) -> list[str]:
    """Rule names that scored an unresolved near-tie against this ticket's
    title+body — extra signal for the clarity check below. Soft-fail → []."""
    try:
        from aiforge_core.runtime import repo_rules
        import os
        query = f"{getattr(ticket, 'title', '') or ''}\n{getattr(ticket, 'body', '') or ''}"
        md = getattr(ticket, "metadata", None) or {}
        globs = md.get("scope_allowlist_globs") or []
        if isinstance(globs, str):
            globs = [g.strip() for g in globs.splitlines() if g.strip()]
        _, ambiguous = repo_rules.collect_or_ask(
            os.environ.get("AIFORGE_REPO_ROOT", ""), globs, query)
        return [" or ".join(f"'{r.name}'" for r in group)
               for group in ambiguous]
    except Exception:  # noqa: BLE001
        return []
```

Update `_ask_llm` (lines 39-54) to accept and fold in the ambiguous list:

```python
def _ask_llm(ticket, ambiguous: list[str] | None = None) -> list[str]:
    from aiforge_core.llm.client import complete
    user = f"Title: {ticket.title}\n\nRequest:\n{ticket.body or ''}"
    if ambiguous:
        user += ("\n\nNote: these repo rules matched with near-equal "
                 "confidence and could not be auto-selected: "
                 + "; ".join(ambiguous) + ". If it matters to the "
                 "implementation, ask which one applies.")
    out = complete("triage", [
        {"role": "system", "content": _SYS},
        {"role": "user", "content": user},
    ], temperature=0.0, max_tokens=400) or ""
    text = out.strip()
    if not text or text.upper().startswith("CLEAR"):
        return []
    qs = []
    for line in text.splitlines():
        q = line.strip().lstrip("-*0123456789. ").strip()
        if q and "?" in q:
            qs.append(q)
    return qs[:3]
```

Update `maybe_clarify` (lines 57-81) to compute and pass ambiguous candidates:

```python
def maybe_clarify(ticket) -> bool:
    """Return True if the run was HALTED to ask the user (caller should
    stop processing this ticket); False to proceed with the pipeline."""
    if not _interactive(ticket) or _clarified(ticket):
        return False
    try:
        ambiguous = _ambiguous_candidates(ticket)
        questions = _ask_llm(ticket, ambiguous)
    except Exception as exc:  # noqa: BLE001
        log.warning("clarify.skip ticket=%s err=%s", ticket.identifier, exc)
        return False
    if not questions:
        # clear enough — mark so we never re-ask, then proceed.
        tickets_mod.update_status(ticket.id, "in_progress", role="clarify",
                                  metadata_patch={"clarified": True})
        return False
    body = "I need a bit more detail before I start:\n" + "\n".join(
        f"- {q}" for q in questions)
    tickets_mod.add_event(ticket.id, "clarify", "clarification", body,
                          {"questions": questions})
    tickets_mod.update_status(
        ticket.id, "blocked", role="clarify",
        metadata_patch={"awaiting_input": True, "clarify_questions": questions},
    )
    log.info("clarify.asked ticket=%s n=%d", ticket.identifier, len(questions))
    return True
```

(`_interactive`/`_clarified` checked first, before `_ambiguous_candidates` runs — an autonomous ticket never pays for the rules scan here, matching `test_maybe_clarify_still_skips_autonomous_tickets`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/python/test_clarify_ambiguous.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/clarify.py tests/python/test_clarify_ambiguous.py
git commit -m "feat: surface ambiguous rule matches to the interactive clarify gate"
```

---

### Task 7: `adk_runner.py` — trigger-aware rules + non-blocking notice for autonomous tickets

**Files:**
- Modify: `aiforge_core/runtime/adk_runner.py:811-871`
- Test: `tests/python/test_adk_runner_ambiguous_notice.py` (new)

Switches the ticket-scoped rules collection from `repo_rules.collect()` to `repo_rules.collect_or_ask()`. For an **interactive** ticket, `clarify.py` (Task 6) already asked before this code runs — no further action needed here. For an **autonomous** ticket (no `interactive` flag), never block: emit a non-blocking `ambiguous_rule_match` event on the ticket trace, same pattern as `turn_router.py`'s existing notice.

- [ ] **Step 1: Write the failing test**

Create `tests/python/test_adk_runner_ambiguous_notice.py`:

```python
"""Autonomous tickets never block on ambiguous rule matches — best-guess is
already applied by collect_or_ask; this only checks the non-blocking notice
event fires (and does NOT fire for interactive tickets, which clarify.py
already handled before this code runs)."""
from __future__ import annotations

from types import SimpleNamespace

import aiforge_core.runtime.adk_runner as ar


def _ticket(interactive=False):
    return SimpleNamespace(id=7, identifier="T-7", title="Deploy",
                           body="deploy release now", project=None,
                           metadata={"interactive": interactive})


def test_autonomous_ambiguous_rule_emits_notice(monkeypatch):
    from aiforge_core.runtime.repo_rules import Rule
    r1 = Rule(name="deploy-staging", globs=(), always=False, body="b",
              source="s", triggers=("deploy",))
    r2 = Rule(name="deploy-prod", globs=(), always=False, body="b",
              source="s", triggers=("deploy",))
    monkeypatch.setattr(
        "aiforge_core.runtime.repo_rules.collect_or_ask",
        lambda *a, **k: ("rendered rules", [[r1, r2]]))
    events = []
    monkeypatch.setattr(
        "aiforge_core.tickets.store.add_event",
        lambda *a, **k: events.append((a, k)))
    ar._emit_ambiguous_rule_notice(_ticket(interactive=False),
                                   [[r1, r2]])
    assert len(events) == 1
    assert events[0][0][2] == "ambiguous_rule_match"


def test_interactive_ticket_no_notice(monkeypatch):
    from aiforge_core.runtime.repo_rules import Rule
    r1 = Rule(name="deploy-staging", globs=(), always=False, body="b",
              source="s", triggers=("deploy",))
    r2 = Rule(name="deploy-prod", globs=(), always=False, body="b",
              source="s", triggers=("deploy",))
    events = []
    monkeypatch.setattr(
        "aiforge_core.tickets.store.add_event",
        lambda *a, **k: events.append((a, k)))
    ar._emit_ambiguous_rule_notice(_ticket(interactive=True), [[r1, r2]])
    assert events == []   # clarify.py already handled it — no double notice
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/python/test_adk_runner_ambiguous_notice.py -v`
Expected: FAIL — `AttributeError: module 'aiforge_core.runtime.adk_runner' has no attribute '_emit_ambiguous_rule_notice'`

- [ ] **Step 3: Implement in `adk_runner.py`**

Add a small helper near the top of the rules-collection block (just before line 811, i.e. before `rules_md = ""`):

```python
def _emit_ambiguous_rule_notice(ticket, ambiguous: list) -> None:
    """Autonomous tickets never block on an ambiguous rule match (an
    interactive ticket already got asked via clarify.py before this code
    runs) — best-guess is already baked into rules_md by collect_or_ask;
    this only surfaces a visible, non-blocking notice on the trace."""
    if not ambiguous:
        return
    md = getattr(ticket, "metadata", None) or {}
    if md.get("interactive"):
        return
    for group in ambiguous:
        names = " or ".join(f"'{r.name}'" for r in group)
        tickets_mod.add_event(
            ticket.id, "pipeline", "ambiguous_rule_match",
            f"Matched rules ambiguous: {names} — picked highest-priority, "
            f"say so if wrong.", {"candidates": [r.name for r in group]})
```

Replace lines 811-818 (the `rules_md = ""` / `try: ... repo_rules.collect(...)` block):

```python
    rules_md = ""
    try:
        from aiforge_core.runtime import repo_rules
        _query = ""
        if ticket is not None:
            _query = (f"{getattr(ticket, 'title', '') or ''}\n"
                      f"{getattr(ticket, 'body', '') or ''}")
        rules_md, _ambiguous_rules = repo_rules.collect_or_ask(
            os.environ.get("AIFORGE_REPO_ROOT", ""), _scope_seed, _query)
        if ticket is not None:
            _emit_ambiguous_rule_notice(ticket, _ambiguous_rules)
    except Exception as exc:  # noqa: BLE001
        log.debug("repo_rules collect failed: %s", exc)
```

`_emit_ambiguous_rule_notice` is a module-level function (not nested) — insert it immediately before `async def _run_pipeline(...)` at line 781, the function that encloses the rules-collection block being modified.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/python/test_adk_runner_ambiguous_notice.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the broader adk_runner suite to check no regression**

Run: `python3 -m pytest tests/python/ -k adk_runner -q`
Expected: PASS, no new failures beyond any pre-existing unrelated ones

- [ ] **Step 6: Commit**

```bash
git add aiforge_core/runtime/adk_runner.py tests/python/test_adk_runner_ambiguous_notice.py
git commit -m "feat: trigger-aware rules + non-blocking ambiguity notice for autonomous tickets"
```

---

### Task 8: Full suite verification + push

**Files:** none (verification only)

- [ ] **Step 1: Run the full targeted test set from Tasks 1-7 together**

Run:
```bash
python3 -m pytest tests/python/runtime/test_skills_select_or_ask.py \
  tests/python/runtime/test_workflows_select_or_ask.py \
  tests/python/runtime/test_repo_rules_triggers.py \
  tests/python/test_chat_agent_rules_context.py \
  tests/python/test_rule_capture_triggers.py \
  tests/python/test_clarify_ambiguous.py \
  tests/python/test_adk_runner_ambiguous_notice.py \
  tests/python/test_enhancer_architect_context.py \
  tests/api/test_rule_capture_api.py -v
```
Expected: all PASS, zero failures

- [ ] **Step 2: Run the full project test suite to catch any wider regression**

Run: `python3 -m pytest -q` (skip/note any pre-existing unrelated failures, e.g. the known `datetime.UTC`/py3.10-vs-3.11 environment mismatch — do not treat those as caused by this work; confirm by checking they also fail on `git stash`)

- [ ] **Step 3: Push**

```bash
git push origin main
```

(Or open a PR if working in a feature branch/worktree — confirm with the user which they prefer before pushing directly to `main`, consistent with how prior sessions in this repo have operated.)
