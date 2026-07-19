"""Enhancer / Architect / Decompose planning + workspace baseline helpers.

Split from ``parallel_subtasks.py`` (mechanical move, behaviour identical)."""
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

# ─────────────── Parallel chat mode (decompose → fan-out → merge) ──────────

_DECOMPOSE_SYS = (
    "You are a planner. Split the task into 3-8 subtasks that run IN PARALLEL. "
    "CRITICAL: each subtask must own a DISTINCT file (or files) — NO two subtasks "
    "may edit the same file, or they will merge-conflict. Put the target file in "
    "the goal, e.g. 'db.py: SQLite store + models'. One file per concern "
    "(db.py, models.py, slug.py, routes.py, main.py, test_app.py, README.md). "
    "Output ONLY: {\"subtickets\": [{\"slug\": \"kebab-id\", \"goal\": "
    "\"<file>: <what>\"}, ...]}. No prose."
)


_ENHANCE_SYS = (
    "You are a senior engineer assistant that cleans up and contextualizes "
    "user requests. First decide the request's intent:\n"
    "- BUILD/CHANGE request (add, fix, build, refactor, etc.): rewrite it as "
    "a clear, concrete build spec — 1-2 lines of goal, then the key "
    "components/files and acceptance criteria as tight bullets.\n"
    "  PIN EVERY AMBIGUITY: separate agents write the tests and the code from "
    "this spec IN ISOLATION, so anything you leave vague they will interpret two "
    "DIFFERENT ways and the tests won't match the code. Replace each vague "
    "quantity or behavioral boundary with ONE exact, testable rule — 'retries a "
    "few times before dropping' → 'retries a failing task up to max_retries "
    "times (default 3) — i.e. 1 initial attempt + up to 3 retries = 4 total — "
    "then drops it'; 'large'/'fast' → a number; and spell out the SHAPE of any "
    "shared data (e.g. a task is a dict with keys id:int, payload, retries:int). "
    "Leave nothing an isolated test-writer and code-writer could read two ways.\n"
    "- INFORMATIONAL/exploratory request (a question about the repo, code, "
    "or how something works — nothing to build or change): restate it as a "
    "single clear, well-formed question, folding in any relevant context. Do "
    "NOT invent build components, files, or acceptance criteria for a "
    "question, and do NOT answer the question yourself.\n"
    "- INTEGRATION/ACTION request (create a JIRA ticket, create/update a "
    "Confluence page, send an email, open a PR, etc.): keep the EXACT action "
    "and target the user named. Do NOT convert it into a code/file build or a "
    "markdown document, do NOT invent files/acceptance criteria, and NEVER "
    "swap the target (a JIRA ticket stays a JIRA ticket — not a doc). Just "
    "clean up the wording.\n"
    "ABSOLUTE RULE: never change the DELIVERABLE TYPE the user explicitly "
    "named, and never fabricate that the user 'clarified' or 'changed their "
    "mind' — they said what they said.\n"
    "Never respond by saying nothing was found, asking the user where to "
    "search, or requesting clarification — if context is sparse, restate the "
    "original request as-is with correct spelling and grammar. Keep it "
    "short. Output ONLY the rewritten request, no preamble."
)


def _orchestrator_timeout_s() -> int:
    """Wall-clock budget for the blocking pre-stream orchestrator LLM calls
    (enhancer / architect / decompose). A hung endpoint must not block every
    non-trivial chat turn for minutes under the default 600s × retries.

    Default 180s: slow *thinking* enhancer models (e.g. qwythos) burn
    300-600 reasoning tokens before emitting the spec and clock 60-150s on
    a real request — a 30s budget timed them out and silently fell back to
    the RAW prompt, dropping all memory/history enrichment. 180s lets a
    reasoning model finish while still bounding a truly hung endpoint.
    Tunable via AIFORGE_ENHANCER_TIMEOUT_S (default 180)."""
    try:
        return max(1, int(os.environ.get("AIFORGE_ENHANCER_TIMEOUT_S", "180")))
    except (TypeError, ValueError):
        return 30


def _enhancer_disabled() -> bool:
    return os.environ.get("AIFORGE_ENHANCER_DISABLE", "").strip().lower() \
        in ("1", "true")


def _enhancer_min_chars() -> int:
    """Pure-length floor: below this many chars a prompt is trivial-by-length
    (no build signal can fit). Kept VERY low so short real imperatives ("add a
    test", "fix the typo in app.py") fall through and ARE enhanced — only the
    whole-message conversational set short-circuits greetings/acks.
    Tunable via AIFORGE_ENHANCER_MIN_CHARS (default 8)."""
    try:
        return max(0, int(os.environ.get("AIFORGE_ENHANCER_MIN_CHARS", "8")))
    except (TypeError, ValueError):
        return 8


# Conversational / non-build openers — greetings, thanks, acks, short meta
# questions. Matched case-insensitively against the (stripped) prompt START.
_CONVERSATIONAL = (
    "hi", "hii", "hey", "hello", "yo", "sup", "gm", "good morning",
    "good evening", "good afternoon", "thanks", "thank you", "thx", "ty",
    "ok", "okay", "cool", "nice", "great", "got it", "sounds good",
    "yes", "yep", "yeah", "no", "nope", "lol", "haha", "bye", "cheers",
    "who are you", "what can you do", "how are you", "what's up", "whats up",
)


def _whole_conversational(low: str) -> bool:
    """True only when the WHOLE message is conversational — a greeting/ack and
    nothing else. Matches a multi-word opener directly (``head == pat``, e.g.
    "good morning", "thank you") OR a string of single-word acks (e.g.
    "ok thanks", "yeah cool"). Crucially it does NOT fire on ack-PREFIXED real
    instructions like "ok, refactor X" (the "refactor"/"X" tokens aren't acks)."""
    import re
    head = low.rstrip("!.?, ")
    if head in _CONVERSATIONAL:
        return True
    toks = [t for t in re.split(r"[\s,]+", head) if t]
    return bool(toks) and all(t in _CONVERSATIONAL for t in toks)


def _is_trivial_prompt(prompt: str) -> bool:
    """True when ``prompt`` is too short to carry a build signal, or the WHOLE
    message is conversational/non-build — so the enhancer (memory fan-out + an
    LLM call) is skipped. Keeps latency low and avoids reshaping chit-chat into
    a fake build spec, WITHOUT swallowing short real imperatives ("add a test")
    or ack-prefixed instructions ("ok, refactor X")."""
    p = (prompt or "").strip()
    if not p:
        return True
    low = p.lower()
    # Pure-length floor (very low): only the shortest fragments. Real short
    # imperatives are longer than this and fall through to be enhanced.
    if len(p) < _enhancer_min_chars():
        return True
    # Whole-message conversational opener (greeting/ack only), any length.
    if len(p) < 64 and _whole_conversational(low):
        return True
    return False


# Change 1 — concrete-prompt skip. A SHORT single-line imperative that already
# names a file + action ("fix the bug in app.py") is already a build spec; the
# enhancer's "rewrite as a build spec" LLM call just adds serial latency. Skip
# it (return the raw prompt) — conservative: only when CLEARLY concrete.
_ACTION_VERBS = (
    "fix", "add", "update", "change", "remove", "rename", "refactor",
    "implement", "write", "create", "delete", "edit", "move",
)
_VERB_RE = re.compile(r"\b(?:" + "|".join(_ACTION_VERBS) + r")\b", re.I)
# A token carrying a code file extension ("app.py", "src/parse.ts"). We
# require a REAL extension (not a bare slash token): matching any "X/Y" path
# over-fired on conceptual slash-phrases like "TCP/IP", "client/server",
# "CI/CD", "read/write" — those name no file, so a verb + one of those wrongly
# skipped enhancement and lost the memory/README context-fold. Concrete now
# means "names an actual code file".
_FILE_EXT_RE = re.compile(
    r"[\w./-]+\.(?:py|js|ts|tsx|jsx|java|go|rs|md|json|ya?ml|sql)\b", re.I)
# Multi-part connectors that mean "enhance, don't skip" (a list / sequence).
_MULTIPART_RE = re.compile(r"\band\b|\bthen\b|;| & ", re.I)


def _enhancer_skip_concrete_enabled() -> bool:
    """Change 1 gate. Default ENABLED; ``AIFORGE_ENHANCER_SKIP_CONCRETE=0``
    (or false/no/off) force-enhances every non-trivial prompt again."""
    return os.environ.get("AIFORGE_ENHANCER_SKIP_CONCRETE", "1") \
        .strip().lower() not in ("0", "false", "no", "off")


def _is_concrete_prompt(prompt: str) -> bool:
    """True when ``prompt`` is a SHORT, single-line-ish imperative that already
    names a concrete file (extension or path separator) AND carries an action
    verb — i.e. it's already actionable and does NOT need the enhancer LLM.

    Conservative by design (err toward enhancing): a vague, multi-part, or long
    prompt returns False so its context still gets folded. Multi-part
    (``and``/``then``/``;``/``&``), multi-line, >200-char, and prompts that name
    no actual code file are all rejected."""
    p = (prompt or "").strip()
    if not p or len(p) > 200:
        return False
    if "\n" in p:                       # multi-line → not a simple one-liner
        return False
    low = p.lower()
    if _MULTIPART_RE.search(low):       # list / sequence → enhance instead
        return False
    if not _VERB_RE.search(low):        # no action verb → not an imperative
        return False
    return bool(_FILE_EXT_RE.search(p))  # must name an actual code file


def _memory_block(prompt: str, repo: str | None) -> str:
    """RELEVANT MEMORY block from unified recall (memory + ticket + code RAG).
    Cheap, soft-fail — never raises, capped ~1200 chars."""
    try:
        from aiforge_core.memory import unified_query
        res = unified_query.query(prompt, repo=repo, limit=5) or {}
        hits = res.get("hits") or []
        lines: list[str] = []
        for h in hits:
            txt = (h.get("text") or "").strip()
            if txt:
                lines.append(f"- {txt}")
        if not lines:
            return ""
        block = "\n".join(lines)
        return "RELEVANT MEMORY:\n" + block[:1200]
    except Exception:  # noqa: BLE001
        return ""


def _history_block(history: list[dict] | None) -> str:
    """RECENT CONVERSATION block: last ~3 turns excluding the current (last)
    user message. Soft-fail, capped ~800 chars."""
    try:
        if not history:
            return ""
        prior = history[:-1]            # drop the current user message
        recent = prior[-3:]
        lines: list[str] = []
        for m in recent:
            role = (m.get("role") or "").strip() or "user"
            content = (m.get("content") or "").strip()
            if content:
                lines.append(f"{role}: {content}")
        if not lines:
            return ""
        block = "\n".join(lines)
        return "RECENT CONVERSATION:\n" + block[:800]
    except Exception:  # noqa: BLE001
        return ""


def _readme_block(cwd: str | None) -> str:
    """REPO README block: head of a README in ``cwd``. Soft-fail, capped
    ~800 chars. Empty when no README present."""
    try:
        if not cwd:
            return ""
        for name in ("README.md", "README.rst", "README"):
            path = os.path.join(cwd, name)
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    head = f.read(800)
                head = head.strip()
                if head:
                    return f"REPO README ({name}):\n{head}"
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _enhance(prompt: str, *, history: list[dict] | None = None,
             cwd: str | None = None, repo: str | None = None) -> str:
    """Layer-1 step 1: fix spelling/grammar, write proper sentences, RECALL
    context (memory + recent conversation + repo README), and fold it all into
    a clear, concrete build spec the planner/doer can act on.

    Backward compatible: existing callers pass just ``prompt``. Falls back to
    the raw ``prompt`` on any error or empty output. Disable entirely via
    ``AIFORGE_ENHANCER_DISABLE=1``."""
    if _enhancer_disabled():
        return prompt
    # Triviality / intent gate: greetings, thanks, short questions and other
    # non-build chit-chat are returned UNCHANGED — skip the memory fan-out and
    # the LLM call (latency) and don't reshape conversational turns into fake
    # build specs.
    if _is_trivial_prompt(prompt):
        return prompt
    # Integration/ACTION request (create a JIRA ticket, Confluence page, send an
    # email, open a PR) — hand it through UNCHANGED. The enhancer's job is to shape
    # BUILD specs; reshaping an action request only risks flipping the deliverable
    # (a JIRA ticket → a doc) and adds latency. The ReAct agent has the tools.
    if re.search(r"\b(jira|confluence|ticket|issue|pull request|\bpr\b|"
                 r"merge request|\bmr\b|email|e-mail|slack|page)\b", prompt, re.I) \
            and re.search(r"\b(create|make|open|file|send|raise|draft|add|update|"
                          r"comment)\b", prompt, re.I):
        return prompt
    # Concrete-prompt short-circuit (Change 1): a short single-line imperative
    # that already names a file + action is already actionable — skip the
    # enhancer LLM call (serial-model latency) and hand the raw prompt straight
    # to the ReAct loop. Gated by AIFORGE_ENHANCER_SKIP_CONCRETE (default on).
    if _enhancer_skip_concrete_enabled() and _is_concrete_prompt(prompt):
        return prompt
    # Gather context — each block is independently soft-failing.
    blocks = [b for b in (
        _memory_block(prompt, repo),
        _history_block(history),
        _readme_block(cwd),
    ) if b]
    context = ("\n\n".join(blocks)) if blocks else ""
    user_msg = (
        f"USER REQUEST:\n{prompt}\n\n"
        + (context + "\n\n" if context else "")
        + "Fix spelling and grammar, write proper sentences, and fold any of "
          "the context above that is relevant. Follow the system "
          "instructions above to decide build spec vs. restated question. "
          "Output ONLY the rewritten request."
    )
    try:
        from aiforge_core.llm import client
        out = client.complete("enhancer", [
            {"role": "system", "content": _ENHANCE_SYS},
            {"role": "user", "content": user_msg}], max_tokens=2048,
            timeout_s=_orchestrator_timeout_s())
        out = (out or "").strip()
        # DEGENERATE-SPEC GUARD: the enhancer is a single point of failure —
        # everything downstream (architect → subtasks → verification) builds
        # against its output. A collapsed or identifier-dropping rewrite must
        # never silently replace the user's ask; fall back to the raw prompt.
        _bad = _spec_degenerate(prompt, out)
        if _bad:
            log.warning("enhancer output rejected (%s) — using raw prompt", _bad)
            return prompt
        return out or prompt
    except Exception:  # noqa: BLE001
        return prompt


def _spec_degenerate(prompt: str, out: str) -> str | None:
    """Reason the enhanced spec is UNUSABLE, else None. Deterministic checks
    only: (a) collapse — the rewrite lost most of a non-trivial ask; (b)
    identifier loss — the prompt named concrete files/symbols and the rewrite
    kept NONE of them (a spec that dropped every anchor builds the wrong
    thing)."""
    if not out:
        return None                     # empty already handled by caller
    if len(prompt) >= 80 and len(out) < max(40, int(len(prompt) * 0.3)):
        return f"collapsed to {len(out)} chars from a {len(prompt)}-char ask"
    import re as _re
    anchors = set(_re.findall(r"\b[\w-]+\.[A-Za-z]{1,4}\b", prompt))  # files
    anchors |= set(_re.findall(r"\b[a-z]+_[a-z_]+\b", prompt))        # snake ids
    anchors = {a for a in anchors if len(a) > 4}
    if anchors and not any(a.lower() in out.lower() for a in anchors):
        return f"dropped every named anchor ({sorted(anchors)[:4]}…)"
    return None


# Public alias for clear imports elsewhere (api.py, etc.).
enhance = _enhance


_ARCHITECT_SYS = (
    "You are the architect. Given a build spec, design the FILE STRUCTURE **and "
    "the exact public API of each file**, because each file is implemented by a "
    "SEPARATE worker in isolation — they can only agree if you fix the shared "
    "contract now. Files must be DISJOINT (single responsibility). Honor any "
    "provided skills, workflows, and repo rules.\n\n"
    "DERIVE EVERYTHING FROM THE SPEC. Every file path, module name, class, and "
    "function MUST come from the modules and functions the SPEC names — do NOT "
    "invent unrelated files, and do NOT copy names from the example below (it "
    "shows JSON FORMAT ONLY, not content). If the spec says a `dates` module with "
    "`days_between`, design `dates.py` exposing `days_between` — never a `game.py` "
    "or `storage.py` the spec never mentioned. When the spec adds ONE module to an "
    "existing package, design ONLY that module's file(s) + its test(s).\n\n"
    "DEPENDENCY CLUSTERING — critical for correctness. Tightly-coupled logic that "
    "shares mutable state or arbitrary conventions (the same constants, the same "
    "in-memory model, the same matrix/state machine) MUST live in ONE file owned "
    "by ONE worker — do NOT atomise a coupled subsystem across files, or separate "
    "workers invent conflicting conventions that never reconcile. Rule: if two "
    "units must edit or assume the same state/constants, COLLAPSE them into a "
    "single file. Give a separate file only to a genuinely DECOUPLED concern (a "
    "persistence layer, a CLI/entrypoint, rendering behind a clean interface). "
    "Prefer a few cohesive files over many fragile ones.\n\n"
    "For every file give its exact PUBLIC API: the class names, function "
    "signatures, and module-level constants that OTHER files import or call — "
    "spelled EXACTLY as everyone must use them (one canonical name per thing). "
    "Use real signatures (names, params, return types where knowable).\n\n"
    "ALWAYS include, in the SAME file list: (a) a TEST file for EVERY code "
    "module (unit tests that exercise its public API), (b) at least one "
    "INTEGRATION test that drives the whole thing end-to-end, and (c) the "
    "project's build/manifest file (pyproject.toml / package.json / go.mod / "
    "pom.xml / Cargo.toml as fits the language). The tests are what lets the "
    "build be verified — never omit them.\n\n"
    "Output ONLY JSON, no prose. The example shows FORMAT ONLY — replace every "
    "name with names DERIVED FROM THE SPEC:\n"
    "{\"files\": [{\"path\": \"<module_from_spec>.py\", \"purpose\": \"<what the "
    "spec says this module does>\", \"api\": [\"def <function_from_spec>(...) -> "
    "<type>\"]}, {\"path\": \"tests/test_<module_from_spec>.py\", \"purpose\": "
    "\"unit-test <module_from_spec>\", \"api\": []}, "
    "{\"path\": \"pyproject.toml\", \"purpose\": \"build manifest\", \"api\": []}]}"
)


def _architect_context(spec: str, cwd: str | None) -> str:
    """SKILLS / WORKFLOWS / REPO RULES for the architect — via the SHARED
    context bundle so the rule source (repo_rules + md_store, query-gated)
    matches every other path. Was `repo_rules.collect` = a divergent rule
    source. Each block capped ~1000 chars."""
    from aiforge_core.runtime import context_bundle as _cb
    b = _cb.build_bundle(cwd or ".", spec, want_repo_map=False,
                         want_summary=False, want_prefs=False)
    parts: list[str] = []
    if b.skills_md:
        parts.append("SKILLS:\n" + b.skills_md.strip()[:1000])
    if b.workflows_md:
        parts.append("WORKFLOWS:\n" + b.workflows_md.strip()[:1000])
    if b.rules_md:
        parts.append("REPO RULES:\n" + b.rules_md.strip()[:1000])
    return "\n\n".join(parts)


_PLAN_CODE_EXTS = {"py", "java", "js", "ts", "tsx", "go", "rs", "kt", "rb",
                   "c", "cpp", "cs", "php"}


# Compiled languages — their cross-module contracts must line up to COMPILE, so
# fragmentation is far more fragile than in Python/JS. Tighter cap for these.
_COMPILED_CODE_EXTS = {"java", "go", "rs", "kt", "c", "cpp", "cc", "cs"}


def _max_compiled_modules() -> int:
    """Tighter module cap for a COMPILED-language plan (default 3). Java/Go/Rust
    isolated agents diverge on the exact type contracts needed to compile;
    keeping a coupled subsystem in ONE module avoids the mismatch. Raise
    AIFORGE_ARCHITECT_MAX_MODULES_COMPILED for a genuinely large compiled build."""
    try:
        return max(1, int(os.environ.get(
            "AIFORGE_ARCHITECT_MAX_MODULES_COMPILED", "3")))
    except ValueError:
        return 3


def _max_code_modules() -> int:
    """Cap on NON-TEST code modules in one plan. Finer decompose is WORSE on a
    local model — it over-splits one responsibility (a single queue into
    ``core.py`` + ``queue_ordering.py`` + ``worker_retry.py``) and the isolated
    workers then diverge on names/imports the reconcile can't stitch. A tight cap
    forces the architect to consolidate coupled logic into cohesive modules
    (coarser = safer). Raise AIFORGE_ARCHITECT_MAX_MODULES for a genuinely large
    build. Tests + manifests don't count — only implementation modules."""
    try:
        return max(1, int(os.environ.get("AIFORGE_ARCHITECT_MAX_MODULES", "4")))
    except ValueError:
        return 4


def _module_cap_for(paths: list[str]) -> tuple[list[str], int]:
    """NON-test code modules in ``paths`` + the applicable cap. Compiled languages
    punish fragmentation HARDER (cross-module type contracts — generics, nested-
    type constructors, signatures — must line up to even COMPILE), so a compiled
    plan gets the tighter :func:`_max_compiled_modules` cap."""
    code = [p for p in paths
            if "." in p and p.rsplit(".", 1)[-1].lower() in _PLAN_CODE_EXTS
            and "test" not in p.lower()]
    compiled = any(p.rsplit(".", 1)[-1].lower() in _COMPILED_CODE_EXTS
                   for p in code)
    cap = (min(_max_code_modules(), _max_compiled_modules()) if compiled
           else _max_code_modules())
    return code, cap


def _coalesce_code_modules(files: list[dict]) -> tuple[list[dict], int]:
    """HARD-enforce the module cap the architect keeps IGNORING in its re-ask:
    deterministically merge excess NON-test code modules down to the cap, at PLAN
    time (before any code is written, so it's safe). Symbols are PRESERVED (union
    of every merged module's ``api``) — they just live in fewer files; the module
    contract + SPEC api-contract carry the merged mapping, so a test importing a
    moved symbol still resolves. Tests / manifests / config files are untouched.
    Returns ``(new_files, n_modules_removed)`` (0 when already within the cap)."""
    def _p(f):
        return str(f.get("path") or "").strip().lstrip("/")
    paths = [_p(f) for f in files]
    code_paths, cap = _module_cap_for(paths)
    if len(code_paths) <= cap:
        return files, 0
    code_set = set(code_paths)
    code = [f for f in files if _p(f) in code_set]
    others = [f for f in files if _p(f) not in code_set]
    code.sort(key=_p)                          # stable, keeps same-dir adjacency
    buckets: list[list[dict]] = [[] for _ in range(cap)]
    n = len(code)
    for i, f in enumerate(code):
        buckets[i * cap // n].append(f)        # even contiguous split into `cap`
    merged: list[dict] = []
    for b in buckets:
        if not b:
            continue
        # keep the shallowest/shortest path as the canonical merged module name.
        rep = min(b, key=lambda f: (_p(f).count("/"), len(_p(f))))
        api: list[str] = []
        for f in b:
            for a in (f.get("api") or []):
                if a and a not in api:
                    api.append(a)
        purpose = "; ".join(str(f.get("purpose") or "") for f in b
                            if f.get("purpose")).strip("; ")[:250]
        merged.append({"path": _p(rep), "purpose": purpose or "combined module",
                       "api": api})
    return others + merged, n - len(merged)


def _validate_plan(files: list[dict]) -> tuple[list[dict], list[str]]:
    """Deterministic sanity gate on the architect's file plan — the plan is a
    single point of failure (every subtask builds against it), so structural
    defects must be caught BEFORE the fan-out, not discovered by 10 workers.
    Returns ``(sanitized_files, issues)``: hard defects (dupes, escaping
    paths) are FIXED in the sanitized list; soft defects (no tests, language
    soup, absurd size) are reported for a semantic reask."""
    issues: list[str] = []
    seen: set[str] = set()
    clean: list[dict] = []
    for f in files:
        p = str(f.get("path") or "").strip().lstrip("/")
        if not p:
            continue
        if p.startswith("..") or "/../" in f"/{p}/":
            issues.append(f"path escapes the workspace: {p!r} (dropped)")
            continue
        # HYPHEN sanitize: a Python module file with a hyphen in its stem
        # (`task-queue.py`) is UNIMPORTABLE — `import task-queue` is a syntax
        # error — so an isolated worker writes it and every `from .task-queue
        # import …` fails. Rename the stem's hyphens to underscores (dir parts +
        # extension untouched); the architect's api/imports reference the module
        # NAME, which the doer derives from this path.
        if p.rsplit(".", 1)[-1].lower() == "py" and "-" in os.path.basename(p):
            d, b = os.path.split(p)
            stem, _dot, ext = b.rpartition(".")
            fixed = os.path.join(d, stem.replace("-", "_") + "." + ext)
            issues.append(f"invalid python module name {p!r} → {fixed!r} "
                          "(hyphens aren't importable)")
            p = fixed
        if p in seen:
            issues.append(f"duplicate path: {p!r} (deduped)")
            continue
        seen.add(p)
        clean.append({**f, "path": p})
    paths = [f["path"] for f in clean]
    exts = {p.rsplit(".", 1)[-1].lower() for p in paths if "." in p}
    code_exts = exts & _PLAN_CODE_EXTS
    if len(paths) > 40:
        issues.append(f"{len(paths)} files is a dump, not a plan — collapse "
                      "coupled concerns (aim well under 40)")
    # Over-fragmentation gate: too many NON-TEST code modules → the architect
    # atomised a coupled subsystem. Re-ask to consolidate (coarser = safer on a
    # local model; finer split diverges and won't reconcile).
    _code_modules, _cap = _module_cap_for(paths)
    if len(_code_modules) > _cap:
        issues.append(
            f"{len(_code_modules)} code modules is over-fragmented for one build "
            f"— CONSOLIDATE coupled logic into at most {_cap} cohesive modules "
            "(e.g. ONE queue.py, not core.py + queue_ordering.py + worker_retry.py). "
            "Give a separate file only to a genuinely DECOUPLED concern "
            "(persistence, CLI/entrypoint). Keep every test + the manifest.")
    if code_exts and not any("test" in p.lower() for p in paths):
        issues.append("plan has code modules but NO test files — every code "
                      "module needs a test file in the SAME plan")
    if len(code_exts - {"js", "ts", "tsx"}) > 2:
        issues.append(f"plan mixes {sorted(code_exts)} languages — a single "
                      "build uses the spec's one stack")
    return clean, issues


class _ArchFileSpec(BaseModel):
    path: str
    purpose: str = ""
    api: list[str] = []


class _ArchitectPlan(BaseModel):
    files: list[_ArchFileSpec] = []


def _architect(spec: str, *, cwd: str | None = None) -> list[dict]:
    """Orchestrator agent 2: design the file structure (disjoint files), guided
    by the repo's skills/workflows/rules. Returns [{path, purpose}, ...] — the
    single source of truth for the split. Backward compatible (cwd optional).
    Uses structured_complete (Pydantic-validated, schema-prompt + reask) —
    replaces the old lossy ``re.search(r"{.*}")`` scrape that silently
    returned [] on any malformed reply."""
    context = ""
    try:
        context = _architect_context(spec, cwd)
    except Exception as exc:  # noqa: BLE001
        log.debug("architect context gather failed: %s", exc)
    user_msg = spec + (("\n\n" + context) if context else "")
    try:
        from aiforge_core.llm.structured import structured_complete

        def _ask(msg: str) -> list[dict]:
            plan = structured_complete("architect", [
                {"role": "system", "content": _ARCHITECT_SYS},
                {"role": "user", "content": msg}],
                _ArchitectPlan, max_tokens=4000,
                timeout_s=_orchestrator_timeout_s())
            return [{"path": f.path, "purpose": f.purpose, "api": f.api}
                    for f in plan.files if (f.path or "").strip()]

        files = _ask(user_msg)
        # PLAN GATE: the architect is a single point of failure — validate the
        # plan structurally BEFORE the fan-out, and give the model ONE semantic
        # reask naming the exact defects. Hard defects (dupes, escapes) are
        # sanitized either way; a still-broken retry ships the sanitized plan
        # with a warning rather than stalling the run.
        files, issues = _validate_plan(files)
        if issues:
            log.warning("architect plan issues (reasking once): %s", issues)
            retry = _ask(user_msg
                         + "\n\nYOUR PREVIOUS PLAN HAD DEFECTS — produce a "
                           "corrected plan fixing EVERY one of these:\n- "
                         + "\n- ".join(issues))
            retry, retry_issues = _validate_plan(retry)
            if retry and len(retry_issues) < len(issues):
                files, issues = retry, retry_issues
            if issues:
                log.warning("architect plan still imperfect after reask "
                            "(shipping sanitized): %s", issues)
        # HARD cap: the re-ask above is ADVISORY and local models routinely ignore
        # it — so if the plan STILL over-fragments, coalesce the excess modules
        # deterministically (plan-time, symbol-preserving) rather than fan out an
        # uncompilable split. Disable with AIFORGE_ARCHITECT_HARD_CAP=0.
        if os.environ.get("AIFORGE_ARCHITECT_HARD_CAP", "1") not in ("0", "false"):
            files, _removed = _coalesce_code_modules(files)
            if _removed:
                log.info("architect over-fragmented past the cap — coalesced "
                         "%d module(s) deterministically (symbols preserved)",
                         _removed)
        return files
    except Exception as exc:  # noqa: BLE001
        log.warning("architect step failed: %s", exc)
        return []


def _module_contract(files: list[dict]) -> str:
    """A shared symbol→module map injected into EVERY subtask's brief.

    The #1 parallel-decompose failure on a local model: each subtask builds in an
    isolated worktree knowing only ITS own file's api, so one subtask's
    ``__init__.py`` writes ``from .queue import TaskQueue`` while the subtask that
    actually defines ``TaskQueue`` put it in ``core.py`` — the imports don't line
    up and the reconciled package won't even import. Pinning WHERE each shared
    symbol lives removes the guess: a subtask importing a symbol reads this map
    and uses the EXACT module that defines it. Tests + api-less files are omitted
    (nothing imports from them)."""
    lines: list[str] = []
    for f in files:
        path = str(f.get("path") or "").strip().lstrip("/")
        api = [str(a) for a in (f.get("api") or []) if a]
        if not path or path.startswith("tests/") or not api:
            continue
        lines.append(f"- `{path}` defines: " + "; ".join(api))
    if len(lines) < 2:
        return ""            # nothing cross-module to coordinate
    return ("PROJECT MODULE MAP — when you import a symbol another module owns, "
            "import it from EXACTLY the module named below; NEVER invent a module "
            "name or assume a symbol lives in a differently-named file:\n"
            + "\n".join(lines))


def _plan_files(files: list[dict]) -> list[dict]:
    """Architect file list → one subtask per file (guaranteed distinct files).

    The slug must be UNIQUE within the plan: it names the worktree dir + branch,
    so two files sharing a basename (``a/db.py`` + ``b/db.py``) slugging to the
    same ``db`` would collide on one worktree → two workers clobber each other.
    On a slug collision we disambiguate with a short hash of the FULL path.

    Every subtask's goal also carries the shared MODULE MAP (:func:`_module_contract`)
    so isolated worktrees can't diverge on where a shared symbol lives — the
    parallel-decompose cohesion fix."""
    import hashlib
    contract = _module_contract(files)
    out, seen_paths, seen_slugs = [], set(), set()
    for f in files:
        path = str(f.get("path") or "").strip().lstrip("/")
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        slug = _slugify(path.rsplit("/", 1)[-1].rsplit(".", 1)[0] or path)
        if slug in seen_slugs:
            # Same basename as an earlier file — append a short stable hash of
            # the full path so the worktree dir/branch stays unique.
            suffix = hashlib.sha1(path.encode("utf-8")).hexdigest()[:6]
            slug = f"{slug}-{suffix}"
        seen_slugs.add(slug)
        _api = [str(a) for a in (f.get("api") or []) if a]
        out.append({"slug": slug, "path": path, "api": _api,
                    "goal": f"{path}: {f.get('purpose') or 'implement'}"
                            + (" | MUST expose EXACTLY: " + "; ".join(_api) if _api else "")
                            + (("\n\n" + contract) if contract else "")})
    return out


def _decompose(prompt: str, tries: int = 2) -> list[dict]:
    """Planner LLM call → subtasks list (JSON array or markdown phases).
    Retries once: a single shot occasionally returns an unparseable format on a
    local model, so we try again before giving up."""
    from aiforge_core.runtime.subtasks_callback import _extract_subtickets
    for attempt in range(max(1, tries)):
        try:
            from aiforge_core.llm import client
            out = client.complete("planner", [
                {"role": "system", "content": _DECOMPOSE_SYS},
                {"role": "user", "content": prompt}], max_tokens=1500,
                timeout_s=_orchestrator_timeout_s())
            subs = _extract_subtickets(out)
            if len(subs) >= 2:
                return subs
        except Exception as exc:  # noqa: BLE001
            log.warning("parallel decompose attempt %d failed: %s", attempt, exc)
    return []


def _ensure_git_workspace(cwd: str) -> str:
    """Make ``cwd`` a git repo with a committed baseline so worktrees can branch
    off it. Returns the base branch name."""
    os.makedirs(cwd, exist_ok=True)
    if _git(["rev-parse", "--git-dir"], cwd).returncode != 0:
        _git(["init"], cwd)
        _git(["config", "user.email", "aiforge@local"], cwd)
        _git(["config", "user.name", "aiforge"], cwd)
    # A fresh workspace is born with the agent's own artifacts gitignored.
    ensure_artifact_gitignore(cwd)
    # need at least one commit for `worktree add <base>` to resolve
    if _git(["rev-parse", "HEAD"], cwd).returncode != 0:
        readme = os.path.join(cwd, ".aiforge-workspace")
        if not os.path.exists(readme):
            with open(readme, "w") as f:
                f.write("aiforge chat workspace\n")
        # .gitignore is the committed baseline (the workspace marker is
        # excluded); excludes keep any stray junk out of the baseline too.
        _git(["add", "-A", "--", ".", *_EXCLUDE_PATHSPECS], cwd)
        _git(["commit", "-m", "workspace baseline"], cwd)
    cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return (cur.stdout or "").strip() or "main"


def _is_managed_workspace(cwd: str) -> bool:
    """True only when ``cwd`` is an AIForge-OWNED throwaway workspace — a chat
    session dir (…/chat-workspaces/session-N) or a ticket worktree
    (…/.aiforge-worktrees/…). A user-pinned real project is NOT managed and must
    never have its working tree auto-committed."""
    try:
        p = os.path.realpath(cwd) + os.sep
    except Exception:  # noqa: BLE001
        return False
    return (
        (os.sep + "chat-workspaces" + os.sep + "session-") in p
        or (os.sep + ".aiforge-worktrees" + os.sep) in p
    )


def _commit_turn_baseline(cwd: str) -> str:
    """Ensure ``cwd`` is a git repo and return a HEAD sha to diff this turn
    against. For an AIForge-MANAGED workspace we also commit the current tree so
    a reused workspace's leftover files (a previous task's edits) fold into the
    baseline instead of being mistaken for THIS turn's work. For a USER-PINNED
    repo we do NOT touch the index/history — staging + committing the user's
    uncommitted WIP onto their branch every turn is destructive; we just read
    HEAD and let the working-tree diff show their changes as before. Returns ''
    only if git is entirely unusable."""
    try:
        _ensure_git_workspace(cwd)
        if _is_managed_workspace(cwd):
            # gitignore keeps artifacts out; --allow-empty just pins HEAD.
            _git(["add", "-A", "--", ".", *_EXCLUDE_PATHSPECS], cwd)
            _git(["commit", "--allow-empty", "-q", "-m", "pre-turn baseline"], cwd)
        return (_git(["rev-parse", "HEAD"], cwd).stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""

# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._worktree import _git, _slugify, log
