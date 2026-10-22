# Recipes, Targets, Scope Net — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harness-enforced (1) per-project command recipes with a zero-LLM route, (2) verbatim stored user targets with an enforced board item, (3) a scope net that flags model-added work — per spec `docs/superpowers/specs/2026-10-22-recipes-targets-scope-design.md`.

**Architecture:** Three new runtime modules (`runtime/recipes.py`, `runtime/targets.py`, `runtime/chat_agent/_guards/scope_net.py`) plus small hooks in the chat agent loop, FINAL handling, prompt blocks, API routing and the web UI. Every enforcement is deterministic; the only model calls stay where they are today.

**Tech Stack:** Python 3.12, FastAPI, pytest; React/TypeScript web under `web/`. Package root `aiforge_core/`. Abbreviations: `R` = `aiforge_core/runtime`, `CA` = `R/chat_agent`, `API` = `aiforge_core/api/routes/_chat`, `T` = `tests/python`.

---

## Working rules (owner's, STRICT)

- Work only in this worktree: `/Users/manip/Documents/codeRepo/AIForgeCrew/.worktrees/recipes-targets-scope` (branch `feat/recipes-targets-scope`).
- **Never run Python/tests on the Mac.** Run on the nuc. Sync + run command (from the worktree root):

```bash
rsync -az --delete --exclude .git --exclude .worktrees --exclude .claude --exclude node_modules --exclude .venv --exclude graphify-out --exclude build --exclude dist --exclude web/dist ./ nuc:~/work/af-recipes/ \
&& ssh nuc 'cd ~/work/af-recipes && PYTHONPATH=$PWD timeout 600 /home/ai/aiforge-test/chat-steer/.venv/bin/python -m pytest -q -p no:cacheprovider <TEST PATHS>'
```

- Commit after each task with the message shown. No push, no tag (the owner merges to main at the end).
- New feature switches default **on** in code and are set **off** in `T/conftest.py`; tests that need a feature set its env var on with `monkeypatch.setenv`.
- Item dicts on the board are `{title, status, from_request?, by_harness?, redo?, aliases?, note?}`; this plan adds `enforced`, `proof`, `files`, `self_added`.

## File map

| File | Responsibility |
|---|---|
| `R/recipes.py` (new) | recipes.json store, verb↔command family map, bare-phrase match, sentence parse, offers |
| `R/targets.py` (new) | target capture, storage, TARGETS block, compare detection, re-ask regex |
| `CA/_guards/scope_net.py` (new) | unattached-files computation + FINAL block |
| `CA/_guards/targets.py` (new) | `TargetReaskGuard` |
| `CA/_tools/_misc.py`, `CA/_tools/_skills.py` | `_t_project` / `_t_run_tests` follow recipes |
| `CA/_loop.py` | raw command replace before gates |
| `CA/_obs_text.py` | `[recipe …]` header in observations |
| `CA/_turn/_blocks.py`, `CA/_turn/_convo.py` | RECIPES + TARGETS priority blocks, session rules |
| `CA/_turn/_tasks.py`, `CA/_turn/_items.py`, `CA/_turn/_state.py`, `CA/_turn/_action.py` | enforced items, `files`, `self_added`, target items seeded, compare check |
| `CA/_turn/_finish.py`, `CA/_turn/_done_check.py` | guard, scope net block, recipe offers, done-check text |
| `R/turn_facts_line.py`, `R/handoff.py`, `R/chat_compact.py` | target facts, handoff line, saved-compact pin |
| `CA/_tools/_memory.py` | session-scope rules in the rules block |
| `CA/_native_prompt.py`, `CA/_prompt_text.py`, `CA/_turn/_plan_llm.py` | prompt stance |
| `API/_routing.py`, `API/_producer.py`, `API/_sessions.py`, `R/commands.py` | zero-LLM route, sentence ack, `/recipes`, recipes + revert endpoints |
| `API/_stages.py` | enhancer trims added verbs |
| `web/src/…` | recipe offer card, scope net card, dock flag |

---

## Task 1: `R/recipes.py` — store, families, phrases, sentences

**Files:**
- Create: `aiforge_core/runtime/recipes.py`
- Test: `tests/python/runtime/test_recipes.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/python/runtime/test_recipes.py
import json
import os
import pytest
from aiforge_core.runtime import recipes as R


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_RECIPES", "1")


def test_save_get_remove_round_trip(tmp_path):
    root = str(tmp_path)
    assert R.get(root, "test") is None
    R.save(root, "test", "./scripts/test.sh", cwd=".", aliases=["t"])
    got = R.get(root, "test")
    assert got["cmd"] == "./scripts/test.sh" and got["aliases"] == ["t"]
    data = json.loads((tmp_path / ".aiforge" / "recipes.json").read_text())
    assert "saved" in data["test"]
    assert R.remove(root, "test") is True
    assert R.get(root, "test") is None
    assert R.remove(root, "test") is False


def test_disabled_switch_hides_everything(tmp_path, monkeypatch):
    R.save(str(tmp_path), "test", "pytest -q")
    monkeypatch.setenv("AIFORGE_CHAT_RECIPES", "0")
    assert R.get(str(tmp_path), "test") is None
    assert R.load(str(tmp_path)) == {}


def test_unreadable_store_is_empty(tmp_path):
    d = tmp_path / ".aiforge"
    d.mkdir()
    (d / "recipes.json").write_text("{not json")
    assert R.load(str(tmp_path)) == {}


@pytest.mark.parametrize("cmd,verb", [
    ("pytest -q", "test"), ("python -m pytest", "test"), ("npm test", "test"),
    ("mvn -q test", "test"), ("go test ./...", "test"), ("cargo test", "test"),
    ("make test", "test"), ("mvn -q clean package -DskipTests", "build"),
    ("mvn compile", "build"), ("npm run build", "build"), ("go build ./...", "build"),
    ("cargo build", "build"), ("make", "build"), ("tsc --noEmit", "build"),
    ("ruff check .", "lint"), ("eslint .", "lint"), ("npm run lint", "lint"),
])
def test_family_of_command(cmd, verb):
    assert R.family_of(cmd) == verb


@pytest.mark.parametrize("cmd", [
    "pytest tests/test_x.py::test_y", "pytest -k foo", "go test ./pkg/one",
    "ls", "git status", "python main.py", "mvn -q test -Dtest=Foo",
])
def test_targeted_or_other_commands_have_no_family(cmd):
    assert R.family_of(cmd) is None


def test_rewrite_replaces_a_family_command_with_the_recipe(tmp_path):
    root = str(tmp_path)
    R.save(root, "test", "./scripts/test.sh")
    assert R.rewrite(root, "pytest -q") == ("test", "./scripts/test.sh")
    assert R.rewrite(root, "./scripts/test.sh") is None      # already the recipe
    assert R.rewrite(root, "pytest tests/a.py") is None      # targeted: keep
    assert R.rewrite(root, "mvn compile") is None            # no build recipe


def test_rewrite_can_be_turned_into_a_warning(tmp_path, monkeypatch):
    R.save(str(tmp_path), "test", "./t.sh")
    monkeypatch.setenv("AIFORGE_CHAT_RECIPE_REPLACE", "0")
    assert R.rewrite(str(tmp_path), "pytest") is None
    assert R.warn(str(tmp_path), "pytest") == "test"


@pytest.mark.parametrize("text,verb", [
    ("run tests", "test"), ("Run the tests.", "test"), ("tests", "test"),
    ("compile", "build"), ("build it", "build"), ("please build", "build"),
    ("run it", "run"), ("lint", "lint"), ("deploy", "deploy"),
    ("run-job", "run-job"), ("run the job", "run-job"), ("run run-job", "run-job"),
    ("nightly", "run-job"),
])
def test_bare_phrases_map_to_verbs(tmp_path, text, verb):
    root = str(tmp_path)
    for v in ("test", "build", "run", "lint", "deploy"):
        R.save(root, v, f"./{v}.sh")
    R.save(root, "run-job", "python main.py --job nightly", aliases=["run the job", "nightly"])
    assert R.verb_for_phrase(root, text) == verb


@pytest.mark.parametrize("text", [
    "run tests and fix the failures", "compile the new module I described above",
    "what does run tests do?", "run tests?", "",
])
def test_longer_messages_are_not_bare_phrases(tmp_path, text):
    R.save(str(tmp_path), "test", "./t.sh")
    R.save(str(tmp_path), "build", "./b.sh")
    assert R.verb_for_phrase(str(tmp_path), text) is None


def test_bare_phrase_without_a_recipe_is_none(tmp_path):
    assert R.verb_for_phrase(str(tmp_path), "run tests") is None


@pytest.mark.parametrize("text,verb,cmd", [
    ("remember: compile = ./build.sh", "build", "./build.sh"),
    ("always run tests with ./t.sh", "test", "./t.sh"),
    ("from now on build is make all", "build", "make all"),
    ("recipe run-job = python main.py --job nightly", "run-job", "python main.py --job nightly"),
    ("test means `pytest -q tests/`", "test", "pytest -q tests/"),
])
def test_parse_sentence_saves(text, verb, cmd):
    assert R.parse_sentence(text) == ("save", verb, cmd)


@pytest.mark.parametrize("text,verb", [
    ("forget the test recipe", "test"), ("remove recipe build", "build"),
    ("drop the run-job recipe", "run-job"),
])
def test_parse_sentence_removes(text, verb):
    assert R.parse_sentence(text) == ("remove", verb, "")


@pytest.mark.parametrize("text", [
    "run tests", "the build is slow", "compile = ./build.sh is what the README says, fix the README",
    "remember that the build uses java 21",
])
def test_parse_sentence_ignores_other_text(text):
    assert R.parse_sentence(text) is None


def test_offers_pick_successful_family_commands_without_a_recipe(tmp_path):
    root = str(tmp_path)
    R.save(root, "build", "mvn -q package")
    steps = [
        {"type": "tool", "name": "run_command", "args": {"cmd": "pytest -q"}, "result": {"ok": True, "code": 0}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "mvn -q package"}, "result": {"ok": True, "code": 0}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "ruff check ."}, "result": {"ok": False, "code": 1}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "pytest -q"}, "result": {"ok": True, "code": 0}},
        {"type": "tool", "name": "file_read", "args": {"path": "x"}, "result": {"ok": True}},
    ]
    assert R.offers(root, steps) == [{"verb": "test", "cmd": "pytest -q", "cwd": "."}]


def test_declined_verbs_are_not_offered_again(tmp_path):
    root = str(tmp_path)
    R.decline(root, "test")
    steps = [{"type": "tool", "name": "run_command", "args": {"cmd": "pytest"}, "result": {"ok": True, "code": 0}}]
    assert R.offers(root, steps) == []


def test_help_text_lists_recipes(tmp_path):
    R.save(str(tmp_path), "test", "./t.sh", aliases=["t"])
    text = R.help_text(str(tmp_path))
    assert "test → ./t.sh" in text and "aliases: t" in text
    assert R.help_text(str(tmp_path / "none")) == "No recipes saved for this project."


def test_block_is_short(tmp_path):
    for i in range(14):
        R.save(str(tmp_path), f"v{i}", f"./{i}.sh")
    block = R.block(str(tmp_path))
    assert block.startswith("RECIPES")
    assert block.count("\n") <= 12 and "more:" in block
```

- [ ] **Step 2: Run tests to verify they fail**

Run (via the nuc command above): `tests/python/runtime/test_recipes.py`
Expected: `ModuleNotFoundError: No module named 'aiforge_core.runtime.recipes'`

- [ ] **Step 3: Write `aiforge_core/runtime/recipes.py`**

```python
"""Per-project command recipes: a verb (test, build, run, lint, deploy or a
user-named slug) → the exact command the project uses for it.

The user confirms a recipe once (card after a successful run, or a sentence
like "remember: compile = ./build.sh"); from then on the harness runs that
command for the verb and the model cannot route around it. Store:
``<repo>/.aiforge/recipes.json``. Off switch: ``AIFORGE_CHAT_RECIPES=0``."""
from __future__ import annotations

import json
import os
import re
import time

from aiforge_core.runtime import _atomic

BUILTIN_VERBS = ("test", "build", "run", "lint", "deploy")
_FILE = "recipes.json"
_DECLINED = "recipes-declined.json"
_MAX_BLOCK_LINES = 10

# Command families: whole-command patterns. A command with a path, a -k/-Dtest
# selector or anything after the family head is NOT a family command (the model
# is running one test file on purpose) — see family_of.
_FAMILY = (
    ("test", re.compile(r"^(?:python3? -m )?pytest(?: -[a-zA-Z]+)*$")),
    ("test", re.compile(r"^(?:npm|yarn|pnpm) (?:run )?test$")),
    ("test", re.compile(r"^mvn(?: -[a-zA-Z]+)* (?:test|verify)$")),
    ("test", re.compile(r"^go test(?: \./\.\.\.)?$")),
    ("test", re.compile(r"^cargo test$")),
    ("test", re.compile(r"^make test$")),
    ("test", re.compile(r"^(?:\./)?gradlew? test$")),
    ("build", re.compile(r"^mvn(?: -[a-zA-Z]+)*(?: clean)? (?:compile|package|install)(?: -DskipTests)?$")),
    ("build", re.compile(r"^(?:npm|yarn|pnpm) run build$")),
    ("build", re.compile(r"^go build(?: \./\.\.\.)?$")),
    ("build", re.compile(r"^cargo build(?: --release)?$")),
    ("build", re.compile(r"^make(?: all| build)?$")),
    ("build", re.compile(r"^(?:npx )?tsc(?: --noEmit)?$")),
    ("build", re.compile(r"^(?:\./)?gradlew? (?:build|assemble)$")),
    ("lint", re.compile(r"^ruff(?: check)?(?: \.)?$")),
    ("lint", re.compile(r"^(?:npx )?eslint(?: \.)?$")),
    ("lint", re.compile(r"^(?:npm|yarn|pnpm) run lint$")),
    ("lint", re.compile(r"^flake8$")),
)

_PHRASES = {
    "test": ("test", "tests", "run tests", "run the tests", "run test", "run all tests"),
    "build": ("build", "build it", "compile", "compile it", "run build", "run the build"),
    "run": ("run", "run it", "run the app", "start it", "run the project"),
    "lint": ("lint", "run lint", "run the linter"),
    "deploy": ("deploy", "deploy it", "run deploy"),
}
_POLITE = re.compile(r"^(?:please|pls|now|ok|okay|and|then|just|go ahead and)\s+", re.I)
_SAVE_RE = re.compile(
    r"^(?:remember:?|always|from now on|recipe:?)?\s*"
    r"(?:run |do )?(?P<verb>[a-z][a-z0-9-]{0,30})\s+"
    r"(?:recipe\s+)?(?:=|is|means|with|using|via)\s+"
    r"`?(?P<cmd>[^`\n]{2,300}?)`?\s*[.!]?$", re.I)
_REMOVE_RE = re.compile(
    r"^(?:forget|remove|drop|delete)\s+(?:the\s+)?(?:recipe\s+)?(?P<verb>[a-z][a-z0-9-]{0,30})"
    r"(?:\s+recipe)?\s*[.!]?$", re.I)
_CMD_START = re.compile(r"^(?:\.?/|[a-z][a-z0-9_.-]*)")


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_RECIPES", "1").strip().lower() not in (
        "0", "false", "no", "off")


def replace_enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_RECIPE_REPLACE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _dir(root: str) -> str:
    return os.path.join(root, ".aiforge")


def _path(root: str) -> str:
    return os.path.join(_dir(root), _FILE)


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic.write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def load(root: str) -> dict:
    """verb → {cmd, cwd, aliases, saved}; {} when off or unreadable."""
    if not enabled() or not root:
        return {}
    out = {}
    for verb, row in _read(_path(root)).items():
        if isinstance(row, dict) and str(row.get("cmd") or "").strip():
            out[str(verb)] = {"cmd": str(row["cmd"]).strip(),
                              "cwd": str(row.get("cwd") or "."),
                              "aliases": [str(a) for a in (row.get("aliases") or []) if str(a).strip()],
                              "saved": row.get("saved")}
    return out


def get(root: str, verb: str) -> dict | None:
    return load(root).get(_slug(verb))


def save(root: str, verb: str, cmd: str, *, cwd: str = ".", aliases=None) -> dict:
    data = _read(_path(root))
    row = {"cmd": cmd.strip(), "cwd": cwd or ".", "saved": int(time.time())}
    if aliases:
        row["aliases"] = [str(a).strip().lower() for a in aliases if str(a).strip()]
    data[_slug(verb)] = row
    _write(_path(root), data)
    return row


def remove(root: str, verb: str) -> bool:
    data = _read(_path(root))
    if _slug(verb) not in data:
        return False
    del data[_slug(verb)]
    _write(_path(root), data)
    return True


def decline(root: str, verb: str) -> None:
    p = os.path.join(_dir(root), _DECLINED)
    data = _read(p)
    data[_slug(verb)] = int(time.time())
    _write(p, data)


def _declined(root: str) -> set:
    return set(_read(os.path.join(_dir(root), _DECLINED)))


def _slug(verb: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(verb or "").strip().lower()).strip("-")


def _norm(cmd: str) -> str:
    return " ".join(str(cmd or "").split())


def family_of(cmd: str) -> str | None:
    """The built-in verb a whole command belongs to, or None for targeted or
    unrelated commands."""
    c = _norm(cmd)
    for verb, rx in _FAMILY:
        if rx.match(c):
            return verb
    return None


def _match(root: str, cmd: str) -> tuple[str, str] | None:
    verb = family_of(cmd)
    if not verb:
        return None
    rec = get(root, verb)
    if not rec or _norm(rec["cmd"]) == _norm(cmd):
        return None
    return verb, rec["cmd"]


def rewrite(root: str, cmd: str) -> tuple[str, str] | None:
    """(verb, recipe command) when a raw family command must run as the recipe."""
    if not replace_enabled():
        return None
    return _match(root, cmd)


def warn(root: str, cmd: str) -> str | None:
    """The verb whose recipe the raw command ignores (warning mode only)."""
    if replace_enabled():
        return None
    m = _match(root, cmd)
    return m[0] if m else None


def verb_for_phrase(root: str, text: str) -> str | None:
    """The verb a bare message asks for, when a recipe for it exists."""
    recs = load(root)
    if not recs:
        return None
    t = _norm(text).lower().strip(" .!")
    t = _POLITE.sub("", t)
    if not t or "?" in t or len(t) > 40:
        return None
    for verb, rec in recs.items():
        phrases = set(_PHRASES.get(verb, ())) | set(rec["aliases"])
        phrases |= {verb, f"run {verb}", f"run the {verb}", verb.replace("-", " ")}
        if t in phrases:
            return verb
    return None


def parse_sentence(text: str) -> tuple[str, str, str] | None:
    """("save", verb, cmd) / ("remove", verb, "") for a one-line recipe sentence."""
    t = _norm(text)
    if not t or "\n" in text.strip() or len(t) > 320:
        return None
    m = _REMOVE_RE.match(t)
    if m:
        return "remove", _slug(m.group("verb")), ""
    m = _SAVE_RE.match(t)
    if not m:
        return None
    verb, cmd = _slug(m.group("verb")), m.group("cmd").strip()
    if verb in ("that", "this", "the", "it") or not _CMD_START.match(cmd):
        return None
    if " is what " in t or " says" in t or verb in ("remember", "always"):
        return None
    return "save", verb, cmd


def offers(root: str, steps) -> list[dict]:
    """Recipe offers for this turn: one per family verb that ran green and has
    no recipe and was not declined. Order = first success."""
    if not enabled():
        return []
    have, declined, out, seen = load(root), _declined(root), [], set()
    for s in steps or []:
        if not isinstance(s, dict) or s.get("type") != "tool":
            continue
        if s.get("name") not in ("run_command", "bash", "run_shell", "shell"):
            continue
        res = s.get("result") if isinstance(s.get("result"), dict) else {}
        if res.get("ok") is not True:
            continue
        cmd = _norm((s.get("args") or {}).get("cmd") or (s.get("args") or {}).get("command"))
        verb = family_of(cmd)
        if not verb or verb in have or verb in declined or verb in seen:
            continue
        seen.add(verb)
        out.append({"verb": verb, "cmd": cmd, "cwd": "."})
        if len(out) == 3:
            break
    return out


def help_text(root: str) -> str:
    recs = load(root)
    if not recs:
        return "No recipes saved for this project."
    lines = ["Recipes for this project:"]
    for verb, rec in recs.items():
        extra = f"  (aliases: {', '.join(rec['aliases'])})" if rec["aliases"] else ""
        lines.append(f"- {verb} → {rec['cmd']}{extra}")
    return "\n".join(lines)


def block(root: str) -> str:
    """Prompt block (priority tier). Empty when there are no recipes."""
    recs = load(root)
    if not recs:
        return ""
    lines = ["RECIPES — this project's fixed commands. For these verbs run the "
             "recipe command exactly (the harness replaces a raw command with it):"]
    verbs = list(recs)
    for verb in verbs[:_MAX_BLOCK_LINES]:
        lines.append(f"- {verb} → {recs[verb]['cmd']}")
    if len(verbs) > _MAX_BLOCK_LINES:
        lines.append("more: " + ", ".join(verbs[_MAX_BLOCK_LINES:]))
    return "\n".join(lines)
```

Check that `aiforge_core/runtime/_atomic.py` has `write_text(path, text)` (it is used by `repo_rules.write_rule`). If its signature differs, adapt `_write`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `tests/python/runtime/test_recipes.py`
Expected: all pass. If a family regex fails a parametrized case, fix the regex, not the test.

- [ ] **Step 5: Add the switches to conftest and commit**

In `tests/python/conftest.py`, next to line 62 (`AIFORGE_CHAT_LLM_PLAN`), add:

```python
os.environ.setdefault("AIFORGE_CHAT_RECIPES", "0")
os.environ.setdefault("AIFORGE_CHAT_TARGETS", "0")
os.environ.setdefault("AIFORGE_CHAT_SCOPE_NET", "0")
```

```bash
git add aiforge_core/runtime/recipes.py tests/python/runtime/test_recipes.py tests/python/conftest.py
git commit -m "feat(recipes): per-project command recipes store, families, phrases"
```

---

## Task 2: Tool handlers follow the recipe; raw commands replaced; observation header

**Files:**
- Modify: `aiforge_core/runtime/chat_agent/_tools/_misc.py:102-111` (`_t_project`)
- Modify: `aiforge_core/runtime/chat_agent/_tools/_skills.py:241-243` (`_t_run_tests`)
- Modify: `aiforge_core/runtime/chat_agent/_loop.py:288-310` (`_gated_action`)
- Modify: `aiforge_core/runtime/chat_agent/_obs_text.py:99-120` (`render_command`)
- Test: `tests/python/runtime/test_recipes_tools.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/python/runtime/test_recipes_tools.py
import pytest
from aiforge_core.runtime import recipes as R
from aiforge_core.runtime.chat_agent import _obs_text
from aiforge_core.runtime.chat_agent._tools import _misc as M
from aiforge_core.runtime.chat_agent._tools import _skills as S


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_RECIPES", "1")


@pytest.fixture
def runner(monkeypatch):
    calls = []
    def run(args, cwd):
        calls.append((dict(args), cwd))
        return {"ok": True, "code": 0, "stdout": "ran", "stderr": ""}
    monkeypatch.setattr("aiforge_core.runtime.chat_agent._shell._t_run_command", run)
    return calls


def test_project_test_action_runs_the_recipe(tmp_path, runner, monkeypatch):
    from aiforge_core.runtime.tools import project_runner as pr
    monkeypatch.setattr(pr, "project", lambda **k: pytest.fail("auto-detect must not run"))
    R.save(str(tmp_path), "test", "./scripts/test.sh", cwd="svc")
    res = M._t_project({"action": "test"}, str(tmp_path))
    assert res["ok"] and res["recipe"] == "test"
    assert runner == [({"cmd": "./scripts/test.sh"}, str(tmp_path / "svc"))]


def test_project_detect_and_unmapped_actions_auto_detect(tmp_path, runner, monkeypatch):
    from aiforge_core.runtime.tools import project_runner as pr
    seen = {}
    monkeypatch.setattr(pr, "project", lambda **k: seen.update(k) or {"ok": True})
    R.save(str(tmp_path), "test", "./t.sh")
    M._t_project({"action": "detect"}, str(tmp_path))
    assert seen["action"] == "detect" and runner == []


def test_run_tests_tool_runs_the_recipe(tmp_path, runner, monkeypatch):
    from aiforge_core.runtime.tools import test_runner as tr
    monkeypatch.setattr(tr, "run_tests", lambda **k: pytest.fail("auto-detect must not run"))
    R.save(str(tmp_path), "test", "pytest -q tests/")
    res = S._t_run_tests({"mode": "fast"}, str(tmp_path))
    assert res["ok"] and res["recipe"] == "test"
    assert runner[0][0] == {"cmd": "pytest -q tests/"}


def test_run_tests_without_a_recipe_is_unchanged(tmp_path, runner, monkeypatch):
    from aiforge_core.runtime.tools import test_runner as tr
    monkeypatch.setattr(tr, "run_tests", lambda **k: {"ok": True, "via": "auto"})
    assert S._t_run_tests({}, str(tmp_path))["via"] == "auto"


def test_loop_rewrites_a_raw_family_command(tmp_path):
    from aiforge_core.runtime.chat_agent import _loop
    R.save(str(tmp_path), "test", "./t.sh")
    args = {"cmd": "pytest -q"}
    _loop._apply_recipe("run_command", args, str(tmp_path))
    assert args == {"cmd": "./t.sh", "_recipe": "test", "_recipe_was": "pytest -q"}
    other = {"cmd": "ls"}
    _loop._apply_recipe("run_command", other, str(tmp_path))
    assert other == {"cmd": "ls"}
    fr = {"path": "x"}
    _loop._apply_recipe("file_read", fr, str(tmp_path))
    assert fr == {"path": "x"}


def test_loop_warn_mode_only_marks(tmp_path, monkeypatch):
    from aiforge_core.runtime.chat_agent import _loop
    monkeypatch.setenv("AIFORGE_CHAT_RECIPE_REPLACE", "0")
    R.save(str(tmp_path), "test", "./t.sh")
    args = {"cmd": "pytest -q"}
    _loop._apply_recipe("run_command", args, str(tmp_path))
    assert args == {"cmd": "pytest -q", "_recipe_warn": "test"}


def test_observation_shows_the_recipe_header():
    out = _obs_text.render_command({"ok": True, "code": 0, "stdout": "ok",
                                    "recipe": "test", "recipe_was": "pytest -q"}, 4000)
    assert out.startswith("[recipe test replaced: pytest -q]\nexit 0")
    out2 = _obs_text.render_command({"ok": True, "code": 0, "stdout": "ok", "recipe": "test"}, 4000)
    assert out2.startswith("[recipe test]\nexit 0")
    warn = _obs_text.render_command({"ok": True, "code": 0, "stdout": "ok", "recipe_warn": "test"}, 4000)
    assert "[this project has a test recipe — use it next time]" in warn
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `tests/python/runtime/test_recipes_tools.py`
Expected: FAIL (`KeyError: 'recipe'`, `AttributeError: _apply_recipe`).

- [ ] **Step 3: `_t_project` follows the recipe**

Replace `_t_project` in `CA/_tools/_misc.py:102-111` with:

```python
def _t_project(args: dict, cwd: str) -> dict:
    """Project-level build/test/run. A saved recipe for the action wins over
    stack auto-detection (the user confirmed it once)."""
    action = str(args.get("action", "detect"))
    base = str(args.get("cwd") or cwd)
    hit = _recipe_run(action, base)
    if hit is not None:
        return hit
    try:
        from aiforge_core.runtime.tools.project_runner import project
        return project(action=action, cwd=base,
                       timeout=int(args.get("timeout", 1800)))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _recipe_run(verb: str, cwd: str) -> dict | None:
    """Run the project's recipe for ``verb`` through the shell tool; None when
    there is none (caller falls back to auto-detection)."""
    import os
    from aiforge_core.runtime import recipes, repo_ident
    root = repo_ident.git_toplevel(cwd) or cwd
    rec = recipes.get(root, verb)
    if not rec:
        return None
    from .._shell import _t_run_command
    where = os.path.normpath(os.path.join(root, rec.get("cwd") or "."))
    res = dict(_t_run_command({"cmd": rec["cmd"]}, where) or {})
    res["recipe"] = verb
    return res
```

- [ ] **Step 4: `_t_run_tests` follows the recipe**

Replace `CA/_tools/_skills.py:241-243` with:

```python
def _t_run_tests(args: dict, cwd: str) -> dict:
    from .._tools._misc import _recipe_run
    hit = _recipe_run("test", cwd)
    if hit is not None:
        return hit
    from aiforge_core.runtime.tools.test_runner import run_tests
    return run_tests(mode=str(args.get("mode") or "fast"), pattern=str(args.get("pattern") or ""))
```

(Check the relative import works from `_skills.py`; it lives in the same `_tools` package, so `from ._misc import _recipe_run` is the right form.)

- [ ] **Step 5: Raw command replace in the loop**

In `CA/_loop.py`, add a module-level helper (near the other `_`-helpers above `_gated_action`):

```python
def _apply_recipe(name: str, args: dict, cwd: str) -> None:
    """Before any gate: a raw family command (pytest, mvn test…) becomes the
    project's recipe for that verb. Marks the args so the observation says so."""
    from aiforge_core.runtime import recipes, repo_ident, shell_writes
    if name not in shell_writes.SHELL_TOOLS or not isinstance(args, dict):
        return
    cmd = str(args.get("cmd") or args.get("command") or "")
    if not cmd:
        return
    root = repo_ident.git_toplevel(cwd) or cwd
    hit = recipes.rewrite(root, cmd)
    if hit:
        verb, new = hit
        key = "cmd" if "cmd" in args else "command"
        args["_recipe_was"] = cmd
        args[key] = new
        args["_recipe"] = verb
        return
    warn = recipes.warn(root, cmd)
    if warn:
        args["_recipe_warn"] = warn
```

In `_gated_action` (l.288), insert as the first statement after the `thought` yield (before `_pre_dispatch_gates` at l.308):

```python
    _apply_recipe(name, args, cwd)
```

Then in `CA/_turn/_action.py`, right where the tool result is rendered (l.~684, before `render_observation`), copy the marks onto the result so the observation can show them:

```python
    if isinstance(result, dict) and isinstance(args, dict):
        for k in ("_recipe", "_recipe_was", "_recipe_warn"):
            if args.get(k):
                result[k.lstrip("_")] = args[k]
```

Check `_call_sig` (l.272) is computed before `_apply_recipe`; if it is, recompute `sig` after the rewrite so the repeat-detection keys on the recipe command: `sig = _call_sig(name, args)` right after the call (confirm the helper name at `_loop.py:272`).

- [ ] **Step 6: Observation header**

In `CA/_obs_text.py` `render_command` (l.99), before `parts = [header]` (l.114), build the header:

```python
    head = ""
    if result.get("recipe"):
        head = (f"[recipe {result['recipe']} replaced: {result['recipe_was']}]\n"
                if result.get("recipe_was") else f"[recipe {result['recipe']}]\n")
    elif result.get("recipe_warn"):
        head = (f"[this project has a {result['recipe_warn']} recipe — use it next time]\n")
    header = head + header
```

- [ ] **Step 7: Run tests**

Run: `tests/python/runtime/test_recipes_tools.py tests/python/runtime/test_chat_misc_tools.py tests/python/runtime/test_workflow_run.py`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add -A aiforge_core/runtime/chat_agent tests/python/runtime/test_recipes_tools.py
git commit -m "feat(recipes): project/run_tests follow the recipe; raw family commands replaced"
```

---

## Task 3: Zero-LLM recipe route, sentence ack, `/recipes`, API endpoints

**Files:**
- Modify: `aiforge_core/api/routes/_chat/_routing.py` (add `_recipe_pass`)
- Modify: `aiforge_core/api/routes/_chat/_producer.py:105-145` (`_events`)
- Modify: `aiforge_core/runtime/commands.py:53,161-202` (`/recipes`)
- Modify: `aiforge_core/api/routes/_chat/_sessions.py` (endpoints)
- Modify: `aiforge_core/api/routes/_chat/_turn_events.py:156-158` (persist `recipe_offer`)
- Test: `tests/python/api/test_chat_recipes_route.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/python/api/test_chat_recipes_route.py
import importlib
import pytest
from fastapi.testclient import TestClient
from aiforge_core.runtime import recipes as R


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_RECIPES", "1")


def _drain(gen):
    return list(gen)


def test_a_recipe_sentence_is_saved_without_the_agent(tmp_path):
    from aiforge_core.api.routes._chat import _routing as RT
    pctx = {"done": False}
    evs = _drain(RT._recipe_pass("remember: compile = ./build.sh", str(tmp_path), 1, pctx))
    assert evs[0]["type"] == "message" and "build" in evs[0]["text"] and "./build.sh" in evs[0]["text"]
    assert evs[-1] == {"type": "done"} and pctx["done"] is True
    assert R.get(str(tmp_path), "build")["cmd"] == "./build.sh"


def test_a_bare_phrase_runs_the_recipe_without_the_agent(tmp_path, monkeypatch):
    from aiforge_core.api.routes._chat import _routing as RT
    R.save(str(tmp_path), "test", "echo hi")
    seen = {}
    monkeypatch.setattr(RT, "_run_recipe", lambda cmd, cwd: seen.update(cmd=cmd, cwd=cwd)
                        or {"cmd": cmd, "ok": True, "code": 0, "output": "hi\n"})
    pctx = {"done": False}
    evs = _drain(RT._recipe_pass("run tests", str(tmp_path), 1, pctx))
    assert seen == {"cmd": "echo hi", "cwd": str(tmp_path)}
    assert evs[0]["type"] == "tool" and evs[0]["name"] == "run_command"
    assert evs[1]["type"] == "message" and "exit 0" in evs[1]["text"] and "hi" in evs[1]["text"]
    assert pctx["done"] is True


def test_a_failing_recipe_hands_the_output_to_the_agent(tmp_path, monkeypatch):
    from aiforge_core.api.routes._chat import _routing as RT
    R.save(str(tmp_path), "test", "false")
    monkeypatch.setattr(RT, "_run_recipe", lambda cmd, cwd: {"cmd": cmd, "ok": False, "code": 1, "output": "boom"})
    pctx = {"done": False}
    evs = _drain(RT._recipe_pass("run tests", str(tmp_path), 1, pctx))
    assert pctx["done"] is False
    assert evs[0]["type"] == "tool"
    assert "recipe `test` failed" in pctx["prompt"] and "boom" in pctx["prompt"]


def test_other_messages_pass_through(tmp_path):
    from aiforge_core.api.routes._chat import _routing as RT
    pctx = {"done": False}
    assert _drain(RT._recipe_pass("fix the login bug", str(tmp_path), 1, pctx)) == []
    assert pctx["done"] is False


def test_recipes_slash_command_lists_them(tmp_path):
    from aiforge_core.runtime import commands as C
    R.save(str(tmp_path), "test", "./t.sh")
    assert C.is_builtin("recipes")
    assert "test → ./t.sh" in C.expand("/recipes", str(tmp_path))


def _fresh_app(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    import aiforge_core.api.api as api
    importlib.reload(api)
    from aiforge_core.runtime import chat_store
    chat_store.reset_backend_for_tests()
    return api


def test_recipe_endpoints_save_list_decline_remove(monkeypatch, tmp_path):
    api = _fresh_app(monkeypatch, tmp_path)
    from aiforge_core.runtime import chat_store
    repo = tmp_path / "repo"
    repo.mkdir()
    sid = chat_store.create_session("hello", role="chat", cwd=str(repo))["id"]
    client = TestClient(api.app)
    r = client.post(f"/api/chat/sessions/{sid}/recipes", json={"verb": "test", "cmd": "./t.sh"})
    assert r.status_code == 200 and r.json()["ok"]
    assert client.get(f"/api/chat/sessions/{sid}/recipes").json()["recipes"]["test"]["cmd"] == "./t.sh"
    assert client.post(f"/api/chat/sessions/{sid}/recipes/decline", json={"verb": "build"}).json()["ok"]
    assert client.delete(f"/api/chat/sessions/{sid}/recipes/test").json()["ok"]
    assert client.get(f"/api/chat/sessions/{sid}/recipes").json()["recipes"] == {}
    assert client.post("/api/chat/sessions/9999/recipes", json={"verb": "x", "cmd": "y"}).status_code == 404
```

Confirm `chat_store.create_session` accepts `cwd=`; if it does not, create the session and then set cwd the way `tests/python/api/test_session_compact.py` or `_sessions.py` does.

- [ ] **Step 2: Run tests to verify they fail**

Run: `tests/python/api/test_chat_recipes_route.py`
Expected: `AttributeError: module … has no attribute '_recipe_pass'`.

- [ ] **Step 3: `_recipe_pass` in `_routing.py`**

Add next to `_rule_capture_pass` (l.440):

```python
def _run_recipe(cmd: str, cwd: str) -> dict:
    from aiforge_core.runtime.tools.project_runner import _exec
    return _exec(cmd, cwd, 1800)


def _recipe_pass(prompt: str, cwd: str, session_id, pctx: dict):
    """Model-free recipe handling: a recipe sentence is saved; a bare verb
    phrase runs the recipe and answers with its output. A failing recipe
    hands the output to the agent as the request (pctx['prompt'])."""
    import os
    from aiforge_core.runtime import recipes, repo_ident
    if not recipes.enabled():
        return
    root = repo_ident.git_toplevel(cwd) or cwd
    parsed = recipes.parse_sentence(prompt)
    if parsed:
        what, verb, cmd = parsed
        if what == "remove":
            text = (f"Removed the `{verb}` recipe." if recipes.remove(root, verb)
                    else f"There is no `{verb}` recipe.")
        else:
            recipes.save(root, verb, cmd)
            text = f"Got it — saved recipe `{verb}` = `{cmd}` for this project."
        yield {"type": "message", "text": text}
        yield {"type": "done"}
        pctx["done"] = True
        return
    verb = recipes.verb_for_phrase(root, prompt)
    if not verb:
        return
    rec = recipes.get(root, verb)
    where = os.path.normpath(os.path.join(root, rec.get("cwd") or "."))
    res = _run_recipe(rec["cmd"], where)
    out = str(res.get("output") or "")
    yield {"type": "tool", "name": "run_command", "args": {"cmd": rec["cmd"], "_recipe": verb},
           "result": {"ok": bool(res.get("ok")), "code": res.get("code"), "stdout": out[-4000:],
                      "stderr": "", "recipe": verb}}
    code = res.get("code")
    if res.get("ok"):
        tail = out[-4000:]
        yield {"type": "message",
               "text": f"`{verb}` recipe: `{rec['cmd']}` — exit {code}\n\n```\n{tail}\n```"}
        yield {"type": "done"}
        pctx["done"] = True
        return
    pctx["prompt"] = (f"The `{verb}` recipe `{rec['cmd']}` failed (exit {code}). "
                      f"Fix the cause; do not rerun it blindly.\n\nOutput:\n```\n{out[-6000:]}\n```")
```

- [ ] **Step 4: Wire it into the producer**

In `API/_producer.py` `_events` (l.136-145), before the rule-capture branch:

```python
    pctx_r = {"done": False}
    if not (getattr(pc, "_wake", False) or _cp.is_temporary(pc.session_id)):
        yield from _recipe_pass(pc.prompt, pc.cwd, pc.session_id, pctx_r)
        if pctx_r["done"]:
            return
        if pctx_r.get("prompt"):
            pc.prompt = pctx_r["prompt"]
            if pc.history and pc.history[-1].get("role") == "user":
                pc.history[-1] = {**pc.history[-1], "content": pc.prompt}
```

Import `_recipe_pass` where `_rule_capture_pass` is imported in `_producer.py`. Note the `_cp` import at l.137 must come before this block — move `from aiforge_core.runtime import chat_private as _cp` above it. Add `_recipe_pass` to the stub list in `tests/python/api/test_chat_overlap_enhance.py:88` so that suite keeps passing (stub as `lambda *a, **k: iter(())`).

The `tool` event must be persisted as a step: `_turn_events.py:156-158` already persists `tool`. Add `"recipe_offer"` to that tuple too (Task 4 emits it).

- [ ] **Step 5: `/recipes` built-in**

In `R/commands.py`: `_BUILTINS = ("help", "commands", "recipes")`. In `expand()` (l.~190) before the help branch:

```python
    if name == "recipes":
        from aiforge_core.runtime import recipes
        return recipes.help_text(_repo_root(cwd))
```

Check `_help_text(cmds)` mentions `/recipes` in its built-in list.

- [ ] **Step 6: Endpoints in `API/_sessions.py`**

Next to `chat_session_compact_context` (l.351):

```python
class _RecipeBody(BaseModel):
    verb: str
    cmd: str = ""
    cwd: str = "."
    aliases: list[str] | None = None


def _recipe_root(session_id: int) -> str:
    from fastapi import HTTPException
    from aiforge_core.runtime import chat_store, chat_worktree, repo_ident
    sess = chat_store.get_session(session_id)
    if not sess:
        raise HTTPException(404, f"session {session_id} not found")
    cwd = chat_worktree.workdir_of(sess) or sess.get("cwd") or _default_cwd()
    return repo_ident.git_toplevel(cwd) or cwd


@router.get("/api/chat/sessions/{session_id}/recipes", responses={404: {"description": "Not found"}})
def chat_session_recipes(session_id: int) -> dict:
    from aiforge_core.runtime import recipes
    return {"ok": True, "recipes": recipes.load(_recipe_root(session_id))}


@router.post("/api/chat/sessions/{session_id}/recipes", responses={404: {"description": "Not found"}})
def chat_session_recipe_save(session_id: int, body: _RecipeBody) -> dict:
    from fastapi import HTTPException
    from aiforge_core.runtime import recipes
    if not body.cmd.strip():
        raise HTTPException(400, "cmd is required")
    row = recipes.save(_recipe_root(session_id), body.verb, body.cmd, cwd=body.cwd, aliases=body.aliases)
    return {"ok": True, "verb": body.verb, "recipe": row}


@router.post("/api/chat/sessions/{session_id}/recipes/decline", responses={404: {"description": "Not found"}})
def chat_session_recipe_decline(session_id: int, body: _RecipeBody) -> dict:
    from aiforge_core.runtime import recipes
    recipes.decline(_recipe_root(session_id), body.verb)
    return {"ok": True}


@router.delete("/api/chat/sessions/{session_id}/recipes/{verb}", responses={404: {"description": "Not found"}})
def chat_session_recipe_remove(session_id: int, verb: str) -> dict:
    from aiforge_core.runtime import recipes
    return {"ok": recipes.remove(_recipe_root(session_id), verb)}
```

Use whatever `BaseModel` import `_control.py` uses for `_RestoreBody`.

- [ ] **Step 7: Run tests**

Run: `tests/python/api/test_chat_recipes_route.py tests/python/api/test_chat_route_helpers.py tests/python/api/test_chat_overlap_enhance.py tests/python/api/test_session_compact.py`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add -A aiforge_core tests/python/api/test_chat_recipes_route.py tests/python/api/test_chat_overlap_enhance.py
git commit -m "feat(recipes): zero-LLM recipe route, recipe sentences, /recipes, session endpoints"
```

---

## Task 4: RECIPES prompt block + offer card at FINAL

**Files:**
- Modify: `aiforge_core/runtime/chat_agent/_turn/_blocks.py:348-371`
- Modify: `aiforge_core/runtime/chat_agent/_turn/_convo.py:204-230`
- Modify: `aiforge_core/runtime/chat_agent/_turn/_finish.py:451-464`
- Test: `tests/python/runtime/test_recipes_prompt_final.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/python/runtime/test_recipes_prompt_final.py
import types
import pytest
from aiforge_core.runtime import recipes as R
from aiforge_core.runtime.chat_agent._turn import _blocks, _finish


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_RECIPES", "1")


def test_recipes_block_sits_in_the_priority_tier(tmp_path):
    R.save(str(tmp_path), "test", "./t.sh")
    out = _blocks._prepend_priority_blocks("CORE", [], "", "RULES x", False, False, "",
                                           recipes_text=R.block(str(tmp_path)))
    assert out.index("RULES x") < out.index("RECIPES") < out.index("CORE")


def test_final_emits_recipe_offers_after_done(tmp_path, monkeypatch):
    from aiforge_core.runtime import action_log
    steps = [{"type": "tool", "name": "run_command", "args": {"cmd": "pytest -q"},
              "result": {"ok": True, "code": 0}}]
    monkeypatch.setattr(action_log, "live_steps", lambda sid: steps)
    st = types.SimpleNamespace(session_id=7, cwd=str(tmp_path))
    evs = list(_finish._recipe_offer_events(st, str(tmp_path)))
    assert evs == [{"type": "recipe_offer", "verb": "test", "cmd": "pytest -q", "cwd": "."}]
    R.save(str(tmp_path), "test", "pytest -q")
    assert list(_finish._recipe_offer_events(st, str(tmp_path))) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `tests/python/runtime/test_recipes_prompt_final.py`
Expected: `TypeError: unexpected keyword 'recipes_text'`, `AttributeError: _recipe_offer_events`.

- [ ] **Step 3: Block**

`CA/_turn/_blocks.py:348`: signature becomes

```python
def _prepend_priority_blocks(sys_msg, asks, prefs, rules, analyze_mode,
                             plan_mode, builder, *, recipes_text: str = "",
                             targets_text: str = ""):
```

and between the `prefs` and `rules` prepends (l.366-369):

```python
    if targets_text:                # user-given targets — must be matched, on disk
        sys_msg = targets_text + "\n\n" + sys_msg
    if recipes_text:                # project recipes — fixed commands
        sys_msg = recipes_text + "\n\n" + sys_msg
```

In `_convo.py` `_build_convo` (l.227), pass them:

```python
    from aiforge_core.runtime import recipes as _recipes, repo_ident as _ri
    _root = _ri.git_toplevel(cwd) or cwd
    sys_msg = _prepend_priority_blocks(
        sys_msg, _asks, prefs, rules, analyze_mode, plan_mode, builder,
        recipes_text=_recipes.block(_root),
        targets_text=_targets_text(session_id))
```

with, in `_convo.py`:

```python
def _targets_text(session_id) -> str:
    try:
        from aiforge_core.runtime import targets
        return targets.block(session_id) if session_id is not None else ""
    except Exception:  # noqa: BLE001 — targets module lands in Task 5
        return ""
```

- [ ] **Step 4: Offers at FINAL**

In `CA/_turn/_finish.py`, add:

```python
def _recipe_offer_events(st, cwd: str):
    """Cards offering to save this turn's green build/test commands as recipes."""
    from aiforge_core.runtime import action_log, recipes, repo_ident
    if not recipes.enabled():
        return
    root = repo_ident.git_toplevel(cwd) or cwd
    for offer in recipes.offers(root, action_log.live_steps(getattr(st, "session_id", None))):
        yield {"type": "recipe_offer", **offer}
```

and in `_handle_final` after `yield {"type": "done"}` (l.~458), before `_collect_suggestion`:

```python
        if not (strict_finish or builder or plan_mode or readonly_mode):
            yield from _recipe_offer_events(st, cwd)
```

- [ ] **Step 5: Run tests**

Run: `tests/python/runtime/test_recipes_prompt_final.py tests/python/runtime/test_final_guards_sequence.py tests/python/runtime/test_turn_facts_guards.py`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add -A aiforge_core tests/python/runtime/test_recipes_prompt_final.py
git commit -m "feat(recipes): RECIPES prompt block and save-as-recipe offer after a green run"
```

---

## Task 5: `R/targets.py` — capture, store, block, compare detection

**Files:**
- Create: `aiforge_core/runtime/targets.py`
- Test: `tests/python/runtime/test_targets.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/python/runtime/test_targets.py
import json
import os
import pytest
from aiforge_core.runtime import targets as T

JSON = '{"a": 1, "b": [1, 2, 3], "c": {"d": "e"}}' + " " * 200


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_TARGETS", "1")
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))


def test_cue_plus_fenced_block_is_captured(tmp_path):
    msg = "The endpoint must match this output:\n```json\n" + JSON + "\n```"
    got = T.capture(5, msg)
    assert len(got) == 1
    t = got[0]
    assert t["id"] == "target-1" and t["label"] == "output" and t["path"].endswith("target-1.json")
    assert open(t["path"]).read().strip() == JSON.strip()
    assert T.list(5)[0]["verified"] is None
    assert json.loads(open(os.path.join(T.dir(5), "targets.json")).read())[0]["turn"] == 1


def test_cue_plus_long_paste_without_fence(tmp_path):
    msg = "expected output:\n" + "x,y\n" * 120
    got = T.capture(5, msg)
    assert got[0]["path"].endswith(".csv")


def test_explicit_target_form_works_for_short_payloads():
    got = T.capture(5, "set target: output = ```\nhello\n```")
    assert got and got[0]["label"] == "output"


@pytest.mark.parametrize("msg", [
    "match this output",                       # cue, no payload
    "```\n" + JSON + "\n```",                  # payload, no cue
    "please compare the two files",            # neither
    "the expected output is wrong, fix it",    # cue words, no payload
])
def test_no_capture_without_cue_and_payload(msg):
    assert T.capture(5, msg) == []
    assert T.list(5) == []


def test_same_label_again_makes_a_new_version():
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    T.mark_verified(5, "target-1", "diff ok")
    got = T.capture(5, "actually match this output instead:\n```\n" + JSON.replace("1", "2") + "\n```")
    assert got[0]["id"] == "target-1"
    rows = T.list(5)
    assert len(rows) == 1 and rows[0]["path"].endswith("target-1.v2.json")
    assert rows[0]["verified"] is None and len(rows[0]["versions"]) == 2


def test_different_label_is_a_second_target():
    T.capture(5, "match this input:\n```\n" + JSON + "\n```")
    T.capture(5, "and match this output:\n```\n" + JSON + "\n```")
    assert [t["id"] for t in T.list(5)] == ["target-1", "target-2"]


def test_block_lists_targets_with_path_and_preview():
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    b = T.block(5)
    assert b.startswith("TARGETS")
    assert "target-1" in b and "output" in b and "file_read" in b and '{"a": 1' in b
    assert "Never ask the user to paste" in b and "verified: no" in b
    assert T.block(6) == ""


def test_disabled_switch(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_TARGETS", "0")
    assert T.capture(5, "match this output:\n```\n" + JSON + "\n```") == []
    assert T.block(5) == ""


@pytest.mark.parametrize("cmd,ok", [
    ("diff out.json {p}", True), ("diff {p} out.json", True), ("cmp a {p}", True),
    ("jq -S . {p} > a && jq -S . out.json > b && diff a b", True),
    ("python compare.py {p} out.json", True), ("python -c 'import json; assert json.load(open(\"{p}\")) == x'", True),
    ("sort out.csv | diff - {p}", True), ("git diff --no-index {p} out.json", True),
    ("cat {p}", False), ("diff a b", False), ("ls", False), ("python run.py", False),
])
def test_is_compare(cmd, ok):
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    p = T.list(5)[0]["path"]
    assert T.is_compare(cmd.format(p=p), 5) == ("target-1" if ok else None)
    assert T.is_compare(cmd.format(p=os.path.relpath(p)), 5) == ("target-1" if ok else None)


def test_mark_verified_records_evidence():
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    T.mark_verified(5, "target-1", "diff -q: no output")
    assert T.list(5)[0]["verified"]["evidence"] == "diff -q: no output"
    assert "verified: yes" in T.block(5)


@pytest.mark.parametrize("text,hit", [
    ("Can you paste the expected output again?", True),
    ("Please share the expected JSON so I can compare.", True),
    ("What was the data you gave earlier?", True),
    ("Send me a sample input.", True),
    ("Done: output matches target-1 (diff empty).", False),
    ("I pasted the result below.", False),
])
def test_reask_regex(text, hit):
    assert bool(T.asks_for_target(text)) is hit


def test_board_item_for_a_target():
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    items = T.board_items(5)
    assert items == {"target-1": {"title": "output matches target-1 — check: a compare command on "
                                           "targets/target-1.json ran and passed",
                                  "status": "pending", "enforced": True, "proof": "target-1"}}
    T.mark_verified(5, "target-1", "ok")
    assert T.board_items(5) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `tests/python/runtime/test_targets.py`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `aiforge_core/runtime/targets.py`**

```python
"""User-given targets: data the user pasted with "match this output" and the
like. Stored verbatim on disk per chat, pinned in the prompt every turn, and
tied to an enforced board item that closes only when a compare command on
the file ran and passed. Off switch: ``AIFORGE_CHAT_TARGETS=0``."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from aiforge_core.runtime import _atomic

_CUE = re.compile(
    r"(?:match(?:es|ing)? (?:this|the|below|following|that) (?:new )?(?P<n1>output|input|result|response|data|file)"
    r"|(?P<n2>expected|target) (?:output|result|response|input|data|json|csv)"
    r"|should (?:look|be) (?:like|exactly)"
    r"|must (?:equal|match)"
    r"|compare (?:with|against|to) this"
    r"|set target:?\s*(?P<n3>[a-z][a-z0-9_-]*)?"
    r"|^target:?\s*(?P<n4>[a-z][a-z0-9_-]*)?)", re.I | re.M)
_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.S)
_REASK = re.compile(
    r"(?:paste (?:it|that|the \w+|them)? ?again|share (?:the|your|me the) expected"
    r"|what (?:was|is) the (?:expected|data|output|input)[^.?]*(?:you )?(?:gave|shared|pasted|earlier)"
    r"|send (?:me )?(?:a |the )?(?:sample|expected)|provide the expected|re-?send the)", re.I)
_COMPARE_TOOLS = ("diff", "cmp", "jq", "csvdiff", "git diff --no-index", "compare", "assert", "deepdiff")
_LABELS = ("output", "input", "result", "response", "data", "file")


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_TARGETS", "1").strip().lower() not in ("0", "false", "no", "off")


def min_chars() -> int:
    try:
        return int(os.environ.get("AIFORGE_CHAT_TARGET_MIN_CHARS", "200"))
    except ValueError:
        return 200


def dir(session_id) -> str:  # noqa: A001 — mirrors chat_media.media_dir
    from aiforge_core.runtime import repo_ident
    return os.path.join(repo_ident.chat_scratch_root(), f"session-{session_id}", ".aiforge", "targets")


def _index(session_id) -> str:
    return os.path.join(dir(session_id), "targets.json")


def list(session_id) -> list[dict]:  # noqa: A001
    if not enabled():
        return []
    try:
        with open(_index(session_id), encoding="utf-8") as fh:
            rows = json.load(fh)
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def _save_index(session_id, rows) -> None:
    os.makedirs(dir(session_id), exist_ok=True)
    _atomic.write_text(_index(session_id), json.dumps(rows, indent=2) + "\n")


def _sniff(text: str) -> str:
    t = text.strip()
    if t[:1] in "{[":
        try:
            json.loads(t)
            return "json"
        except ValueError:
            pass
    if t.startswith("<"):
        return "xml"
    lines = [ln for ln in t.splitlines() if ln.strip()]
    if len(lines) >= 2 and all(ln.count(",") == lines[0].count(",") and "," in ln for ln in lines[:20]):
        return "csv"
    if re.match(r"^[\w-]+:\s", t) and ":" in t:
        return "yaml"
    return "txt"


def _payload(text: str, cue_end: int) -> str | None:
    fences = _FENCE.findall(text)
    if fences:
        return max(fences, key=len)
    rest = text[cue_end:].lstrip(" :\n")
    if len(rest) >= min_chars():
        return rest
    return None


def _label(m) -> str:
    for g in ("n1", "n3", "n4"):
        if m.group(g):
            return m.group(g).lower()
    if m.group("n2"):
        return "output"
    return "output"


def capture(session_id, text: str, turn: int | None = None) -> list[dict]:
    """Save the target(s) a user message carries. Returns the new/updated rows."""
    if not enabled() or not text or session_id is None:
        return []
    m = _CUE.search(text)
    if not m:
        return []
    body = _payload(text, m.end())
    if body is None:
        return []
    label = _label(m)
    rows = list(session_id)
    body = body.strip("\n") + "\n"
    ext = _sniff(body)
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    hit = next((r for r in rows if r.get("label") == label), None)
    if hit and hit.get("sha256") == sha:
        return [hit]
    if hit:
        n = len(hit.get("versions") or []) + 1
        fname = f"{hit['id']}.v{n}.{ext}"
        row = hit
    else:
        fname = f"target-{len(rows) + 1}.{ext}"
        row = {"id": f"target-{len(rows) + 1}", "label": label, "versions": [], "turn": turn or len(rows) + 1}
        rows.append(row)
    os.makedirs(dir(session_id), exist_ok=True)
    path = os.path.join(dir(session_id), fname)
    _atomic.write_text(path, body)
    row.update({"path": path, "bytes": len(body.encode("utf-8")), "sha256": sha, "verified": None,
                "created": int(time.time())})
    row["versions"] = (row.get("versions") or []) + [path]
    if turn is not None:
        row["turn"] = turn
    _save_index(session_id, rows)
    return [row]


def mark_verified(session_id, target_id: str, evidence: str) -> None:
    rows = list(session_id)
    for r in rows:
        if r["id"] == target_id:
            r["verified"] = {"at": int(time.time()), "evidence": str(evidence)[:1000]}
    _save_index(session_id, rows)


def _preview(path: str, n: int = 300) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(n + 1)[:n].replace("\n", " ⏎ ")
    except OSError:
        return "(unreadable)"


def block(session_id) -> str:
    rows = list(session_id)
    if not rows:
        return ""
    lines = ["TARGETS — data the user gave to match. Each is on disk, verbatim. Read it "
             "with file_read. Never ask the user to paste or repeat it. A target counts "
             "as matched only after you run a compare command (diff/cmp/jq/python) on the "
             "file and it passes; the harness closes the board item from that command."]
    for r in rows:
        ok = "yes" if r.get("verified") else "no"
        lines.append(f"- {r['id']} ({r['label']}, {os.path.basename(r['path'])}, {r['bytes']} B) "
                     f"— verified: {ok} — full: file_read {r['path']}\n  preview: {_preview(r['path'])}")
    return "\n".join(lines)


def board_items(session_id) -> dict:
    """Enforced board items for unverified targets (slug → item)."""
    out = {}
    for r in list(session_id):
        if r.get("verified"):
            continue
        rel = os.path.join("targets", os.path.basename(r["path"]))
        out[r["id"]] = {"title": f"{r['label']} matches {r['id']} — check: a compare command on {rel} ran and passed",
                        "status": "pending", "enforced": True, "proof": r["id"]}
    return out


def is_compare(cmd: str, session_id) -> str | None:
    """The target id a shell command compares against, else None."""
    c = " ".join(str(cmd or "").split())
    if not c or not any(t in c for t in _COMPARE_TOOLS):
        return None
    for r in list(session_id):
        names = {r["path"], os.path.basename(r["path"]), os.path.relpath(r["path"])}
        if any(n and n in c for n in names):
            return r["id"]
    return None


def asks_for_target(text: str) -> bool:
    return bool(_REASK.search(text or ""))


def facts(session_id) -> list[str]:
    """Facts-line fragments, e.g. 'target-1: matched' / 'target-1: not yet compared'."""
    out = []
    for r in list(session_id):
        out.append(f"{r['id']}: " + ("matched" if r.get("verified") else "not yet compared"))
    return out
```

- [ ] **Step 4: Run tests**

Run: `tests/python/runtime/test_targets.py`
Expected: pass. If `_sniff` mis-types the CSV case, tighten the CSV check (all first 20 non-empty lines have the same comma count ≥ 1).

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/targets.py tests/python/runtime/test_targets.py
git commit -m "feat(targets): verbatim target store, prompt block, compare detection"
```

---

## Task 6: Targets wired into the turn — capture, enforced board items, compare closes, pins

**Files:**
- Modify: `aiforge_core/api/routes/_chat/_message.py:142-172`
- Modify: `aiforge_core/runtime/chat_agent/_turn/_state.py:318-323`
- Modify: `aiforge_core/runtime/chat_agent/_turn/_tasks.py:246-302,366-395`
- Modify: `aiforge_core/runtime/chat_agent/_turn/_action.py:199-200,684-690`
- Modify: `aiforge_core/runtime/chat_agent/_turn/_done_check.py:183-187`
- Modify: `aiforge_core/runtime/handoff.py:94-200`, `aiforge_core/runtime/chat_compact.py:150-156`
- Modify: `aiforge_core/runtime/turn_facts_line.py:169-201`
- Test: `tests/python/runtime/test_targets_turn.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/python/runtime/test_targets_turn.py
import types
import pytest
from aiforge_core.runtime import targets as T
from aiforge_core.runtime.chat_agent._turn import _tasks, _action

JSON = '{"a": 1}' + " " * 200


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_TARGETS", "1")
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))


def _seed(sid=5):
    T.capture(sid, "match this output:\n```\n" + JSON + "\n```")
    board = _tasks.seed_board(["make the endpoint"], "")
    board.update(T.board_items(sid))
    return board


def test_enforced_item_refuses_done_from_the_model():
    board = _seed()
    res, evs = _tasks.apply_progress(board, {"slug": "target-1", "status": "done"})
    assert res["ok"] is False and "compare" in res["error"]
    assert board["target-1"]["status"] == "pending"
    res, _ = _tasks.apply_progress(board, {"slug": "target-1", "status": "running"})
    assert res["ok"] and board["target-1"]["status"] == "running"
    res, _ = _tasks.apply_progress(board, {"slug": "target-1", "status": "done", "_harness": True})
    assert res["ok"] and board["target-1"]["status"] == "done"


def test_enforced_item_cannot_be_removed():
    board = _seed()
    res, _ = _tasks.apply_progress(board, {"slug": "target-1", "remove": True})
    assert res["ok"] is False and "target-1" in board


def test_a_passing_compare_closes_the_item_and_marks_verified():
    board = _seed()
    st = types.SimpleNamespace(session_id=5, board=board, cwd="/x", item_log=None)
    p = T.list(5)[0]["path"]
    evs = _action._note_target_compare(st, "run_command", {"cmd": f"diff out.json {p}"},
                                       {"ok": True, "code": 0, "stdout": ""})
    assert board["target-1"]["status"] == "done"
    assert T.list(5)[0]["verified"]["evidence"].startswith("diff out.json")
    assert evs and evs[0]["type"] == "subtask_update" and evs[0]["status"] == "done"


def test_a_failing_compare_keeps_the_item_open():
    board = _seed()
    st = types.SimpleNamespace(session_id=5, board=board, cwd="/x", item_log=None)
    p = T.list(5)[0]["path"]
    _action._note_target_compare(st, "run_command", {"cmd": f"diff out.json {p}"},
                                 {"ok": False, "code": 1, "stdout": "< a\n> b"})
    assert board["target-1"]["status"] == "pending" and T.list(5)[0]["verified"] is None


def test_turn_pin_carries_the_targets_block():
    _seed()
    st = types.SimpleNamespace(goal="go", steers=[], cwd="/x", file_hashes={}, unlimited=True, session_id=5)
    pin = _tasks.turn_pin(st)
    assert "TARGETS" in pin and "target-1" in pin


def test_handoff_render_lists_targets():
    from aiforge_core.runtime import handoff
    _seed()
    st = types.SimpleNamespace(goal="go", board={}, session_id=5, convo=[], file_hashes={},
                               steers=[], edits_made=0, cwd="/x")
    h = handoff.build_chat(st)
    assert h["targets"] == ["target-1 (output): not yet compared"]
    assert "target-1" in handoff.render(h)


def test_facts_line_mentions_targets(monkeypatch):
    from aiforge_core.runtime import turn_facts_line as F, action_log
    _seed()
    monkeypatch.setattr(action_log, "live_steps", lambda _s: [{"type": "tool", "name": "run_command",
                                                               "args": {"cmd": "ls"}, "result": {"ok": True}}])
    monkeypatch.setattr(F, "_changed", lambda *a, **k: set())
    assert "target-1: not yet compared" in F.suffix(5, "/x", None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `tests/python/runtime/test_targets_turn.py`
Expected: FAIL (`apply_progress` accepts done; `_note_target_compare` missing; …).

- [ ] **Step 3: Capture at message entry**

`API/_message.py` after `_user_msg_id = chat_store.add_message(...)` (l.142):

```python
    try:
        from aiforge_core.runtime import targets as _targets
        _turn_no = sum(1 for r in chat_store.get_messages(session_id) if r.get("role") == "user")
        _captured = _targets.capture(session_id, body.content, turn=_turn_no)
    except Exception as exc:  # noqa: BLE001 — never block the turn on target storage
        log.warning("target capture failed: %s", exc)
        _captured = []
```

Use the module's existing logger name (check the top of `_message.py`). If `_captured` is non-empty, nothing else changes here — the board seed (next step) reads the store.

- [ ] **Step 4: Seed enforced items**

`CA/_turn/_state.py` after `board=seed_board(_asks, _turn_goal(messages))` (l.320) — add, after `st` is built but before it is returned:

```python
    try:
        from aiforge_core.runtime import targets as _targets
        if session_id is not None:
            for slug, item in _targets.board_items(session_id).items():
                st.board.setdefault(slug, item)
            if st.board and any(it.get("enforced") for it in st.board.values()):
                st.board_used = True
    except Exception:  # noqa: BLE001
        pass
```

(`session_id` is in scope in `_build_loop_state`; confirm the name.)

- [ ] **Step 5: `apply_progress` refuses done/remove on enforced items**

In `CA/_turn/_tasks.py` `apply_progress`, after the `_remove` dispatch and before the status write (l.~298), add:

```python
    if slug in board and board[slug].get("enforced") and not args.get("_harness"):
        if status == "done":
            return {"ok": False, "slug": slug, "status": board[slug]["status"],
                    "error": "target items close only on a passing compare command "
                             "(diff/cmp/jq/python) on the target file — run one"}, []
```

In `_remove` (l.224) treat `enforced` like `from_request`: not removable. Also in `board_items(board)` (l.97-100) add `"enforced": bool(it.get("enforced"))` and `"self_added": bool(it.get("self_added"))` to each row.

- [ ] **Step 6: Compare closes the item**

In `CA/_turn/_action.py`, add next to `_note_green_tests` usage (l.685):

```python
def _note_target_compare(st, name, args, result) -> list:
    """A passing compare command on a target file closes its enforced item."""
    from aiforge_core.runtime import shell_writes, targets
    if name not in shell_writes.SHELL_TOOLS or not isinstance(result, dict):
        return []
    sid = getattr(st, "session_id", None)
    cmd = shell_writes.command_of(args or {})
    tid = targets.is_compare(cmd, sid)
    if not tid or result.get("ok") is not True:
        return []
    board = getattr(st, "board", None) or {}
    if tid not in board or board[tid].get("status") == "done":
        return []
    out = str(result.get("stdout") or "")[:400]
    targets.mark_verified(sid, tid, f"{cmd[:200]} → exit 0{(' · ' + out) if out else ''}")
    board[tid]["status"] = "done"
    board[tid]["note"] = "compare passed: " + cmd[:120]
    return [{"type": "subtask_update", "slug": tid, "status": "done"}]
```

and call it right after `_note_green_tests(...)` (l.685), yielding its events the same way other `subtask_update` events are yielded in that function.

- [ ] **Step 7: Pins and summaries**

- `_tasks.turn_pin(st)` (l.366): after the `FILES CHANGED SO FAR` part, append:

```python
    try:
        from aiforge_core.runtime import targets as _targets
        tb = _targets.block(getattr(st, "session_id", None))
        if tb:
            parts.append(tb)
    except Exception:  # noqa: BLE001
        pass
```

- `R/handoff.py` `build_chat(st)` (l.94): add `h["targets"] = [f"{r['id']} ({r['label']}): " + ("matched" if r.get("verified") else "not yet compared") for r in targets.list(st.session_id)]` (guard `session_id` None). In `render(h, …)` (l.147) emit a `targets:` line after `files` when `h.get("targets")`: `"targets: " + "; ".join(h["targets"]) + " — files pinned in TARGETS block"`.
- `R/chat_compact.py` `compact_saved` (l.155): `pin = ((latest[:2000] or "") + "\n\n" + targets.block(session_id)).strip() or None`.
- `_done_check._measured` (l.183-187): after the open-board line add `TARGETS NOT PROVED: target-1 — a compare command must run and pass` for unverified targets of `st.session_id`.
- `R/turn_facts_line.parts` (l.172-201): before the early `return []` at l.196 is NOT the place (no actions = no facts). Append `out.extend(targets.facts(session_id))` just before the final `return out`.

- [ ] **Step 8: Run tests**

Run: `tests/python/runtime/test_targets_turn.py tests/python/runtime/test_board_order_and_rules.py tests/python/runtime/test_plan_llm.py tests/python/runtime/test_structured_compaction.py tests/python/runtime/test_turn_facts_line.py tests/python/runtime/test_chat_long_runs.py`
Expected: pass.

- [ ] **Step 9: Commit**

```bash
git add -A aiforge_core tests/python/runtime/test_targets_turn.py
git commit -m "feat(targets): captured at message entry, enforced board item closed only by a passing compare, pinned through condense and handoff"
```

---

## Task 7: `TargetReaskGuard` at FINAL

**Files:**
- Create: `aiforge_core/runtime/chat_agent/_guards/targets.py`
- Modify: `aiforge_core/runtime/chat_agent/_turn/_finish.py:371-382`
- Test: `tests/python/runtime/test_target_reask_guard.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/python/runtime/test_target_reask_guard.py
import pytest
from aiforge_core.runtime import targets as T
from aiforge_core.runtime.chat_agent._guards import base, targets as tg

JSON = '{"a": 1}' + " " * 200


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_TARGETS", "1")
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))


class St:
    session_id = 5
    role = "chat"
    def __init__(self):
        self.convo = []


def test_guard_sends_back_once_then_labels():
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    st = St()
    g = tg.TargetReaskGuard()
    step = {"text": "Could you paste the expected output again?"}
    sig = list(base._check(g, st, step))
    assert st.convo and "target-1" in st.convo[-1]["content"] and ".json" in st.convo[-1]["content"]
    step2 = {"text": "Please share the expected JSON."}
    list(base._check(g, st, step2))
    assert step2["text"].startswith("⚠")


def test_guard_is_silent_without_targets_or_without_an_ask():
    st = St()
    g = tg.TargetReaskGuard()
    assert g.applies(st) is False
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    assert g.applies(st) is True and g.detect("Done, diff is empty.") == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `tests/python/runtime/test_target_reask_guard.py`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write the guard**

Read `CA/_guards/base.py:18-80` first to match attribute names exactly (`counter`, `strip_body`, `echo_text`, `applies`, `budget`, `detect`, `evidence`, `notice`, `nudge`, `disclaimer`). Then:

```python
"""FINAL guard: the answer asks the user to paste a target again although the
target is on disk. Sends the model back once with the path; then labels."""
from __future__ import annotations

from aiforge_core.runtime import targets


class TargetReaskGuard:
    counter = "target_reask_nudges"
    strip_body = False
    echo_text = False

    def applies(self, st) -> bool:
        self._rows = targets.list(getattr(st, "session_id", None))
        return bool(self._rows)

    def budget(self, st) -> int:
        return 1

    def detect(self, text: str) -> list:
        return ["reask"] if targets.asks_for_target(text) else []

    def evidence(self, st):
        return False

    def notice(self, claims) -> str:
        return "⚠ the answer asks for the target again — it is on disk; sending the agent back"

    def _paths(self) -> str:
        return ", ".join(f"{r['id']} → {r['path']}" for r in self._rows)

    def nudge(self, claims) -> str:
        return ("The target data is already on disk; do not ask the user for it. "
                f"Read it with file_read: {self._paths()}. Then continue the task.")

    def disclaimer(self, claims) -> str:
        return ("⚠ **Harness target check:** the target was given earlier and is on disk "
                f"({self._paths()}); the agent asked for it again.\n\n")
```

- [ ] **Step 4: Register in `_handle_final`**

`CA/_turn/_finish.py:371-382`: import `from .._guards.targets import TargetReaskGuard` and append `TargetReaskGuard()` to the guard list after `RuleBreakGuard(...)`.

- [ ] **Step 5: Run tests**

Run: `tests/python/runtime/test_target_reask_guard.py tests/python/runtime/test_final_guards_sequence.py tests/python/runtime/test_board_order_and_rules.py`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add -A aiforge_core tests/python/runtime/test_target_reask_guard.py
git commit -m "feat(targets): FINAL guard sends the agent back when it asks for a target again"
```

---

## Task 8: Scope net — item files, `self_added`, FINAL block, revert endpoint

**Files:**
- Modify: `aiforge_core/runtime/chat_agent/_turn/_items.py:264-312`
- Modify: `aiforge_core/runtime/chat_agent/_turn/_tasks.py:291-296`
- Create: `aiforge_core/runtime/chat_agent/_guards/scope_net.py`
- Modify: `aiforge_core/runtime/chat_agent/_turn/_finish.py:434-453`
- Modify: `aiforge_core/api/routes/_chat/_control.py` (revert endpoint)
- Modify: `aiforge_core/api/routes/_chat/_turn_events.py:156-158` (persist `scope_net`)
- Test: `tests/python/runtime/test_scope_net.py`, `tests/python/api/test_chat_revert.py`

Attribution rule (refines the spec): a file is **unrequested** when its edit evidence falls in the window of a `self_added` item. Edits in windows of request/steer items, and trailing edits after the last closed item, belong to the request. When item logging is inactive (board < 3 items) the net reports nothing.

- [ ] **Step 1: Write the failing tests**

```python
# tests/python/runtime/test_scope_net.py
import types
import pytest
from aiforge_core.runtime.chat_agent._guards import scope_net as SN
from aiforge_core.runtime.chat_agent._turn import _tasks, _items


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_SCOPE_NET", "1")


def test_model_added_items_are_tagged():
    b = _tasks.seed_board(["add login", "add logout", "write docs"])
    res, _ = _tasks.apply_progress(b, {"slug": "refactor-utils", "title": "refactor utils", "status": "running"})
    assert res["ok"] and b["refactor-utils"]["self_added"] is True
    assert b["part-1"].get("self_added") is None
    rows = {r["slug"]: r for r in _tasks.board_items(b)}
    assert rows["refactor-utils"]["self_added"] is True


def test_a_reworded_request_item_is_not_self_added():
    b = _tasks.seed_board(["add login endpoint", "add logout endpoint", "write docs"])
    res, _ = _tasks.apply_progress(b, {"slug": "login", "title": "add the login endpoint", "status": "running"})
    assert "login" not in b and b["part-1"].get("self_added") is None


def test_unattached_files_come_from_self_added_windows():
    b = _tasks.seed_board(["a", "b", "c"])
    b["extra"] = {"title": "tidy utils", "status": "done", "self_added": True, "files": ["src/util.py", "README.md"]}
    b["part-1"]["status"] = "done"; b["part-1"]["files"] = ["src/login.py", "README.md"]
    st = types.SimpleNamespace(board=b, cwd="/r")
    assert SN.unrequested(st, {"src/login.py", "src/util.py", "README.md", "tests/t.py"}) == ["src/util.py"]


def test_nothing_flagged_without_self_added_items():
    b = _tasks.seed_board(["a", "b", "c"])
    st = types.SimpleNamespace(board=b, cwd="/r")
    assert SN.unrequested(st, {"x.py"}) == []


def test_block_text_and_event():
    st = types.SimpleNamespace(board={}, cwd="/r", head0="abc123", session_id=9)
    assert SN.block(["src/util.py"]) == "\n\n⚠ Unrequested changes: `src/util.py`"
    assert SN.event(st, ["src/util.py"]) == {"type": "scope_net", "paths": ["src/util.py"], "head0": "abc123"}


def test_review_progress_records_item_files(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_ITEM_VERIFY", "0")
    b = _tasks.seed_board(["a", "b", "c"])
    st = types.SimpleNamespace(board=b, cwd="/r", item_log=[("edit", ["/r/src/a.py"])], item_start=0,
                               item_rejects={}, item_prev_start=0, item_last_closed=None,
                               readonly_mode=False, builder="", item_seen={}, file_hashes={}, tree_hashes={})
    b["part-1"]["status"] = "done"
    _items.review_progress(st, {"ok": True, "slug": "part-1", "status": "done"}, [])
    assert b["part-1"]["files"] == ["src/a.py"]
```

```python
# tests/python/api/test_chat_revert.py
import importlib
import subprocess
import pytest
from fastapi.testclient import TestClient

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


def _fresh_app(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    import aiforge_core.api.api as api
    importlib.reload(api)
    from aiforge_core.runtime import chat_store
    chat_store.reset_backend_for_tests()
    return api


def _repo(tmp_path):
    r = tmp_path / "repo"; r.mkdir()
    (r / "a.py").write_text("x = 1\n")
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], GIT + ["commit", "-q", "-m", "init"]):
        subprocess.run(cmd, cwd=r, check=True)
    head0 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, capture_output=True, text=True).stdout.strip()
    return r, head0


def test_revert_restores_tracked_and_deletes_untracked(monkeypatch, tmp_path):
    api = _fresh_app(monkeypatch, tmp_path)
    from aiforge_core.runtime import chat_store
    r, head0 = _repo(tmp_path)
    (r / "a.py").write_text("x = 2\n")
    (r / "new.py").write_text("y\n")
    sid = chat_store.create_session("hello", role="chat", cwd=str(r))["id"]
    client = TestClient(api.app)
    res = client.post(f"/api/chat/sessions/{sid}/revert", json={"paths": ["a.py", "new.py"], "head0": head0})
    assert res.status_code == 200 and res.json()["reverted"] == ["a.py", "new.py"]
    assert (r / "a.py").read_text() == "x = 1\n" and not (r / "new.py").exists()


def test_revert_refuses_paths_outside_the_work_folder(monkeypatch, tmp_path):
    api = _fresh_app(monkeypatch, tmp_path)
    from aiforge_core.runtime import chat_store
    r, head0 = _repo(tmp_path)
    sid = chat_store.create_session("hello", role="chat", cwd=str(r))["id"]
    client = TestClient(api.app)
    res = client.post(f"/api/chat/sessions/{sid}/revert", json={"paths": ["../cfg/x"], "head0": head0})
    assert res.status_code == 200 and res.json()["refused"] == ["../cfg/x"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `tests/python/runtime/test_scope_net.py tests/python/api/test_chat_revert.py`
Expected: FAIL.

- [ ] **Step 3: Record item files and tag self-added**

`CA/_turn/_items.py` l.308: after `item["note"] = _note(facts, status, suffix)` add `item["files"] = list(facts.get("edited") or [])`.

`CA/_turn/_tasks.py` l.296: `board[slug] = {"title": title[:_MAX_TITLE], "status": status or "pending", "self_added": True}` — but only when the add is not a harness add. Check how roles (`slug == "repos"`) and `by_harness` adds reach this line; if they do, guard with `if slug != "repos" and not args.get("by_harness")`. `_same_work` already runs before, so reworded request items never reach this line.

- [ ] **Step 4: `scope_net.py`**

```python
"""FINAL scope net: files the model changed under work it added itself."""
from __future__ import annotations

import os


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_SCOPE_NET", "1").strip().lower() not in ("0", "false", "no", "off")


def unrequested(st, changed) -> list[str]:
    """Changed files whose only item evidence is a self-added board item."""
    board = getattr(st, "board", None) or {}
    own, asked = set(), set()
    for it in board.values():
        files = set(it.get("files") or [])
        (own if it.get("self_added") else asked).update(files)
    return sorted((own - asked) & set(changed or ()))


def block(paths) -> str:
    if not paths:
        return ""
    return "\n\n⚠ Unrequested changes: " + ", ".join(f"`{p}`" for p in paths)


def event(st, paths) -> dict:
    return {"type": "scope_net", "paths": list(paths), "head0": getattr(st, "head0", None)}
```

- [ ] **Step 5: Hook into `_handle_final`**

In `CA/_turn/_finish.py`, inside the `if not (strict_finish or builder or plan_mode):` block that builds the facts line (l.434-440), after `_bg_note = _facts.suffix(...)`:

```python
        from .._guards import scope_net as _sn
        _sn_paths = []
        if _sn.enabled() and not readonly_mode:
            _changed = _facts._changed(cwd, getattr(st, "head0", None),
                                       getattr(st, "session_id", None), _wt_fp0 or "") or set()
            _sn_paths = _sn.unrequested(st, _changed)
            _bg_note = _bg_note + _sn.block(_sn_paths)
```

and after `yield {"type": "done"}`:

```python
        if _sn_paths:
            yield _sn.event(st, _sn_paths)
```

(`_sn_paths` must be initialised to `[]` before the `if not (...)` block so the later yield is safe.) Add `"scope_net"` to the persisted step tuple in `_turn_events.py:156-158`.

- [ ] **Step 6: Revert endpoint**

In `API/_control.py` next to the checkpoint restore (l.58):

```python
class _RevertBody(BaseModel):
    paths: list[str]
    head0: str | None = None


@router.post("/api/chat/sessions/{session_id}/revert", responses={404: {"description": "Not found"}})
def chat_session_revert(session_id: int, body: _RevertBody) -> dict:
    """Undo the unrequested changes the scope net listed: tracked files go back
    to head0 (or HEAD), untracked files are deleted. Paths outside the chat's
    work folder are refused."""
    import os
    import subprocess
    from aiforge_core.runtime import chat_runs, chat_store, chat_worktree
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"session {session_id} not found")
    if chat_runs.is_running(session_id):
        raise HTTPException(409, "a turn is running")
    cwd = chat_worktree.workdir_of(session) or session.get("cwd") or _default_cwd()
    root = os.path.realpath(cwd)
    reverted, refused = [], []
    for p in body.paths:
        full = os.path.realpath(os.path.join(root, p))
        if not full.startswith(root + os.sep):
            refused.append(p)
            continue
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", p], cwd=root,
                                 capture_output=True).returncode == 0
        if tracked:
            ref = body.head0 or "HEAD"
            ok = subprocess.run(["git", "checkout", ref, "--", p], cwd=root, capture_output=True).returncode == 0
        else:
            try:
                os.remove(full)
                ok = True
            except OSError:
                ok = False
        (reverted if ok else refused).append(p)
    return {"ok": not refused, "reverted": reverted, "refused": refused}
```

- [ ] **Step 7: Run tests**

Run: `tests/python/runtime/test_scope_net.py tests/python/api/test_chat_revert.py tests/python/runtime/test_turn_facts_guards.py tests/python/runtime/test_final_guards_sequence.py tests/python/runtime/test_board_order_and_rules.py tests/python/runtime/test_plan_llm.py`
Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add -A aiforge_core tests/python/runtime/test_scope_net.py tests/python/api/test_chat_revert.py
git commit -m "feat(scope-net): flag files changed under model-added work, revert endpoint"
```

---

## Task 9: Prompt stance, enhancer trim, session rules in the rules block

**Files:**
- Modify: `aiforge_core/runtime/chat_agent/_native_prompt.py:28-30`
- Modify: `aiforge_core/runtime/chat_agent/_prompt_text.py:328-330`
- Modify: `aiforge_core/runtime/chat_agent/_turn/_plan_llm.py:61-62`
- Modify: `aiforge_core/api/routes/_chat/_stages.py:130-148`
- Modify: `aiforge_core/runtime/chat_agent/_tools/_memory.py:447,476-506`, `CA/_turn/_convo.py:72-93,212`
- Test: `tests/python/runtime/test_scope_prompt_rules.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/python/runtime/test_scope_prompt_rules.py
import pytest
from aiforge_core.api.routes._chat import _stages
from aiforge_core.runtime.chat_agent import _native_prompt, _prompt_text
from aiforge_core.runtime.chat_agent._turn import _plan_llm


def test_prompts_say_do_only_the_request():
    for src in (_native_prompt.__dict__, _prompt_text.__dict__):
        text = " ".join(v for v in src.values() if isinstance(v, str))
        assert "state the assumption and continue" not in text
        assert "Suggested, not done" in text
    assert "Suggested, not done" in _plan_llm._RULES


def test_enhancer_keeps_clarifications_and_drops_added_steps():
    m = {"role": "user", "content": "fix the login bug in auth.py"}
    enriched = ("Fix the login bug in auth.py (the token expiry check). "
                "Add unit tests for the auth module. Refactor the session store. "
                "Run the existing tests.")
    _stages._augment_user_turn(m, enriched, "", "fix the login bug in auth.py", None)
    assert "token expiry" in m["content"]
    assert "Add unit tests" not in m["content"] and "Refactor the session" not in m["content"]
    assert "Run the existing tests" in m["content"]          # 'run' is not an added-work verb


def test_enhancer_old_behaviour_switch(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_ENHANCE_ADDS", "1")
    m = {"role": "user", "content": "fix the bug"}
    _stages._augment_user_turn(m, "Fix the bug. Add tests.", "", "fix the bug", None)
    assert "Add tests" in m["content"]


def test_session_rules_reach_the_rules_block(monkeypatch):
    from aiforge_core.runtime.rule_capture import _base, _store
    from aiforge_core.runtime.chat_agent._tools import _memory as M
    monkeypatch.setattr(M, "_md_rule_bullets", lambda cwd: [])
    monkeypatch.setattr(M, "_repo_rule_bullets", lambda cwd: [])
    monkeypatch.setattr(M, "_constraint_bullets", lambda cwd: [])
    _base._SESSION_ITEMS["42"] = [{"category": "rule", "scope": "session", "canonical": "for this chat, never touch migrations"}]
    try:
        out = M._rules_context("/r", "anything", session_id=42)
        assert "never touch migrations" in out and out.startswith("RULES")
        assert "never touch migrations" in M.user_rule_lines("/r", session_id=42)
        assert M._rules_context("/r", "anything", session_id=43) == ""
    finally:
        _base._SESSION_ITEMS.pop("42", None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `tests/python/runtime/test_scope_prompt_rules.py`
Expected: FAIL on all four.

- [ ] **Step 3: Prompt stance**

Replace `_native_prompt.py:28-30` with:

```
Do only what the request says. If something else seems needed (a refactor, \
extra tests, a config change), do NOT do it: list it at the end of the answer \
under "Suggested, not done". If the request is ambiguous and the choice changes \
the result, ask with a line that starts with ASK:. A choice that does not change \
the result: pick the plain default and say so in one line.
```

Replace `_prompt_text.py:328-330` ("If the request is clear, proceed. When a choice can be undone…") with:

```
If the request is clear, proceed. Do only what it says; anything else you \
think is needed goes under "Suggested, not done" at the end of the answer, \
not into the code. ASK only when the choices cannot be undone and lead to \
different results.
```

In `_plan_llm._RULES` (l.61-62) extend the last bullet: `"- Do not add work the user did not ask for; extra work the agent thinks is needed is reported as \"Suggested, not done\", never planned. Split a big piece of work into its natural steps only when it really has several.\n"`.

Keep the line continuation style (`\` inside the string) that the files use.

- [ ] **Step 4: Enhancer trim**

In `API/_stages.py` add above `_augment_user_turn`:

```python
_ADD_VERBS = ("add", "create", "write", "refactor", "rename", "remove", "delete", "update",
              "install", "configure", "migrate", "deploy", "document", "introduce", "extract", "split")
_SENT = re.compile(r"(?<=[.!?])\s+")


def _first_verb(sentence: str) -> str:
    words = re.findall(r"[a-zA-Z]+", sentence)
    return words[0].lower() if words else ""


def _trim_added_work(raw: str, enriched: str) -> str:
    """Drop restatement sentences that start with an added-work verb the user's
    own message does not use. The restatement may clarify, never add steps."""
    if os.environ.get("AIFORGE_CHAT_ENHANCE_ADDS", "0").strip().lower() in ("1", "true", "yes", "on"):
        return enriched
    asked = {w.lower() for w in re.findall(r"[a-zA-Z]+", raw)}
    keep = []
    for s in _SENT.split(enriched.strip()):
        v = _first_verb(s)
        if v in _ADD_VERBS and v not in asked and not any(a.startswith(v) for a in asked):
            continue
        keep.append(s)
    return " ".join(keep).strip()
```

and in `_augment_user_turn` (l.142) use `enriched = _trim_added_work(raw, enriched or "")` before the `if enriched and enriched.strip() …` test. Add `import os, re` if missing.

- [ ] **Step 5: Session rules**

`CA/_tools/_memory.py`:
- `_rules_context(cwd: str, query: str = "", session_id=None) -> str`: before combining `blocks`, add

```python
    sess = _session_rule_bullets(session_id)
    if sess:
        blocks.append("Rules for THIS chat:\n" + "\n".join(f"- {b}" for b in sess))
```

with

```python
def _session_rule_bullets(session_id) -> list[str]:
    if session_id is None:
        return []
    from aiforge_core.runtime.rule_capture._base import _SESSION_ITEMS
    out = []
    for it in _SESSION_ITEMS.get(str(session_id), []):
        if it.get("undone"):
            continue
        text = str(it.get("canonical") or "").strip()
        if text:
            out.append(text)
    return out
```

- `user_rule_lines(cwd, session_id=None)` (l.447): extend its list with `_session_rule_bullets(session_id)`.
- `_convo._seed_prompt(messages, cwd, readonly_mode, *, native=False, plan_mode=False, session_id=None)` passes `session_id` to `_rules_context(cwd, last_user, session_id=session_id)`; `_build_convo` (l.212) passes `session_id=session_id`.
- `RuleBreakGuard` (`CA/_guards/rules.py`): where it calls `user_rule_lines(cwd)`, pass `session_id=getattr(st, "session_id", None)`.

- [ ] **Step 6: Run tests**

Run: `tests/python/runtime/test_scope_prompt_rules.py tests/python/api/test_chat_overlap_enhance.py tests/python/runtime/test_board_order_and_rules.py tests/python/runtime/test_plan_llm.py` plus any test that greps prompt text: `ssh nuc 'cd ~/work/af-recipes && grep -rl "state the assumption" tests/ | head'` and run those.
Expected: pass; fix any test that pinned the old prompt sentence by updating its expectation to the new sentence.

- [ ] **Step 7: Commit**

```bash
git add -A aiforge_core tests/python
git commit -m "feat(scope): do-only-the-request stance, enhancer never adds steps, session rules reach the model"
```

---

## Task 10: Web UI — recipe offer card, scope net card, dock flags

**Files:**
- Modify: `web/src/api/chat.ts` (add `saveRecipe`, `declineRecipe`, `revertPaths`)
- Modify: `web/src/views/Chat.tsx` (handle `recipe_offer`, `scope_net`)
- Create: `web/src/views/Chat.RecipeOfferCard.tsx`, `web/src/views/Chat.ScopeNetCard.tsx`
- Modify: `web/src/views/Chat.SubtaskList.tsx`, `web/src/views/Chat.model.ts` (flags)

- [ ] **Step 1: API client**

In `web/src/api/chat.ts` next to `compactContext` (l.120):

```ts
  saveRecipe: (id: number, body: { verb: string; cmd: string; cwd?: string }) =>
    j<{ ok: boolean }>(`/chat/sessions/${id}/recipes`, { method: 'POST', body: JSON.stringify(body) }),
  declineRecipe: (id: number, verb: string) =>
    j<{ ok: boolean }>(`/chat/sessions/${id}/recipes/decline`, { method: 'POST', body: JSON.stringify({ verb, cmd: '' }) }),
  revertPaths: (id: number, paths: string[], head0: string | null) =>
    j<{ ok: boolean; reverted: string[]; refused: string[] }>(`/chat/sessions/${id}/revert`,
      { method: 'POST', body: JSON.stringify({ paths, head0 }) }),
```

Match how `j` sets headers/body in the existing calls (copy `compactContext`'s shape).

- [ ] **Step 2: Cards**

`web/src/views/Chat.RecipeOfferCard.tsx`:

```tsx
import { useState } from 'react';
import { chatApi } from '../api/chat';

export type RecipeOffer = { verb: string; cmd: string; cwd: string };

export default function RecipeOfferCard({ sessionId, offer, onDone }:
  { sessionId: number; offer: RecipeOffer; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const act = async (save: boolean) => {
    setBusy(true);
    try {
      if (save) await chatApi.saveRecipe(sessionId, { verb: offer.verb, cmd: offer.cmd, cwd: offer.cwd });
      else await chatApi.declineRecipe(sessionId, offer.verb);
    } finally { setBusy(false); onDone(); }
  };
  return (
    <div className="recipe-offer" role="group" aria-label="recipe offer">
      <span>Save <code>{offer.cmd}</code> as this project's <b>{offer.verb}</b> recipe?</span>
      <button disabled={busy} onClick={() => act(true)}>Save</button>
      <button disabled={busy} onClick={() => act(false)}>Not this</button>
    </div>
  );
}
```

`web/src/views/Chat.ScopeNetCard.tsx`:

```tsx
import { useState } from 'react';
import { chatApi } from '../api/chat';

export type ScopeNet = { paths: string[]; head0: string | null };

export default function ScopeNetCard({ sessionId, net, onDone }:
  { sessionId: number; net: ScopeNet; onDone: (msg: string) => void }) {
  const [busy, setBusy] = useState(false);
  const revert = async () => {
    setBusy(true);
    try {
      const r = await chatApi.revertPaths(sessionId, net.paths, net.head0);
      onDone(r.refused.length ? `Reverted ${r.reverted.length}; refused: ${r.refused.join(', ')}`
                               : `Reverted ${r.reverted.length} file(s)`);
    } finally { setBusy(false); }
  };
  return (
    <div className="scope-net" role="group" aria-label="unrequested changes">
      <span>⚠ Unrequested changes: {net.paths.map(p => <code key={p}>{p}</code>)}</span>
      <button disabled={busy} onClick={revert}>Revert these</button>
    </div>
  );
}
```

Use the `chatApi` export name that `web/src/api/chat.ts` actually exports (check the import used by `Chat.tsx`).

- [ ] **Step 3: Events in `Chat.tsx`**

Follow the `suggestion` handling (`Chat.tsx:1720` and the render at `:2773`): add state `recipeOffers: RecipeOffer[]` and `scopeNet: ScopeNet | null` on the live turn (or component state, whichever `suggestion` uses), set them on `recipe_offer` / `scope_net` events, clear on a new turn, and render `<RecipeOfferCard>` per offer and `<ScopeNetCard>` under the last assistant bubble, next to `SuggestionChip`.

- [ ] **Step 4: Dock flags**

`web/src/views/Chat.model.ts` `SubtaskItem`: add `enforced?: boolean; self_added?: boolean`. `Chat.SubtaskList.tsx` label (l.77): append ` (added by agent)` when `s.self_added`, and a `🔒` prefix when `s.enforced`.

- [ ] **Step 5: Type-check and build on the nuc**

```bash
rsync … (same command) && ssh nuc 'cd ~/work/af-recipes/web && npm ci --silent && npx tsc --noEmit && npm run build 2>&1 | tail -5'
```

Expected: no tsc errors, build ok. (If `npm ci` needs the Artifactory-only registry on the nuc, use the registry env the nuc's other `af-*` trees use: check `~/work/af-board/web/.npmrc`.)

- [ ] **Step 6: Commit**

```bash
git add web/src
git commit -m "feat(web): recipe offer card, unrequested-changes card with revert, dock flags"
```

---

## Task 11: Related suite, review rounds, live rig, spec note, merge

- [ ] **Step 1: Related suite on the nuc**

```bash
ssh nuc 'cd ~/work/af-recipes && PYTHONPATH=$PWD timeout 1800 /home/ai/aiforge-test/chat-steer/.venv/bin/python -m pytest -q -p no:cacheprovider tests/python/runtime tests/python/api -x -q 2>&1 | tail -15'
```

Expected: pass, except the two known order-dependent fails (`test_no_progress_loop::…`, `test_same_failure_loop::…`) which also fail on untouched main. Any other fail: fix.

- [ ] **Step 2: Update the spec's scope-net attribution rule**

In `docs/superpowers/specs/2026-10-22-recipes-targets-scope-design.md` §3 "Harness check at FINAL", replace items 2-4 with the rule from Task 8 (self-added windows; inactive item logging ⇒ nothing flagged). Commit: `docs: scope-net attribution rule as built`.

- [ ] **Step 3: Code review, two rounds**

Dispatch `superpowers:code-reviewer` on `git diff main...HEAD`. Fix findings, re-run the related suite, dispatch again. Commit fixes as `fix(review): …`.

- [ ] **Step 4: Live rig on the nuc**

Copy `~/work/af-rules-live.sh` to `~/work/af-recipes-live.sh`, port `18987`, `TREE=af-recipes`, `W=$HOME/work/af-recipes-live`, model `qwen3-coder-next`. Seed `$W/repos/shop` with a tiny Python project (one module, `scripts/test.sh` running `pytest -q`, a committed git repo). Turns (one session, send sequentially — adapt `chat()` to reuse `sid`):

1. `The function total() in shop/cart.py must match this output:` + fenced JSON of 300+ chars + `. Change it.` → check `$W/cfg/chat-workspaces/session-1/.aiforge/targets/target-1.json` exists, reply does not ask to paste, board shows 🔒 item.
2. `POST /api/chat/sessions/1/compact-context`, then `continue` → reply reads the file, no re-ask; item closes only after a `diff`/compare tool step with exit 0 (check steps).
3. `run tests` → first time: model runs pytest, a `recipe_offer` event appears; `POST /recipes {verb:test, cmd:...}`; second `run tests` → steps = one `run_command` tool step, no model call (api.log shows no `/v1/chat/completions` for that turn — or use the proxy `~/work/ctxrun/llmproxy.py`).
4. `also rename the README title` as a steer during a 4th task, and check the facts line and any `scope_net` event.

Record results in the memory file (Step 6).

- [ ] **Step 5: Merge to main**

```bash
cd /Users/manip/Documents/codeRepo/AIForgeCrew
git checkout main && git merge --no-ff feat/recipes-targets-scope -m "feat(chat): recipes, targets, scope net — harness-enforced determinism"
```

Push only when the owner says so.

- [ ] **Step 6: Memory**

Write `~/.claude/projects/-Users-manip-Documents-codeRepo/memory/aiforge-recipes-targets-scope-2026-10-22.md` (type project) with: what shipped, switches, rig script, traps found, and add the index line to `MEMORY.md` under AIForge.
