"""The prompt enhancer: whether a request needs rewriting, the context it is
given, and the rewrite itself."""
from __future__ import annotations

import contextlib
import os
import re

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
# TOKENS, not a pattern. "Does this text name a code file" is a suffix test on
# each word, and a quantifier over a user's prompt is the denial-of-service
# shape a scanner asks about — an earlier version of this very line was one.
_CODE_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs",
              ".md", ".json", ".yaml", ".yml", ".sql")


def _names_a_code_file(text: str) -> bool:
    """True when ``text`` mentions something that looks like a real code file
    ("src/app.py"), rather than a slash-phrase like "TCP/IP" or "read/write"."""
    for raw in (text or "").split():
        token = raw.strip("\"'`(),;:[]{}<>").lower()
        stem = token.rsplit("/", 1)[-1]
        if "." not in stem:
            continue
        if any(stem.endswith(ext) and len(stem) > len(ext)
               for ext in _CODE_EXTS):
            return True
    return False
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
    return _names_a_code_file(p)          # must name an actual code file


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
             cwd: str | None = None, repo: str | None = None,
             on_context=None) -> str:
    """Layer-1 step 1: fix spelling/grammar, write proper sentences, RECALL
    context (memory + recent conversation + repo README), and fold it all into
    a clear, concrete build spec the planner/doer can act on.

    Backward compatible: existing callers pass just ``prompt``. Falls back to
    the raw ``prompt`` on any error or empty output. Disable entirely via
    ``AIFORGE_ENHANCER_DISABLE=1``. ``on_context`` (optional) is called once the
    context is gathered, right before the LLM call — a caller's cue to start
    work that can overlap it."""
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
    if on_context is not None:
        # A caller's hook must never cost the spec.
        with contextlib.suppress(Exception):
            on_context()
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


# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._worktree import log
