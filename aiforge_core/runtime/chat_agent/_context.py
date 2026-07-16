from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from ._shell import (_ACTION_RE, _workspace_root)
from ._tools import (_SKIP_DIRS, _cached_find_by_source, _chat_repo_key)

# Loop detection: no fixed step budget — long coding sessions run until
# the agent finishes. We stop only when it's clearly STUCK: the same
# tool+args repeated this many times, or identical model output N times
# in a row. ``_SAFETY_CAP`` is a last-resort runaway guard (very high;
# tune with AIFORGE_CHAT_SAFETY_CAP), not a normal stopping point.
_LOOP_REPEAT = 4
_OUTPUT_REPEAT = 3


_CONDENSE_OPEN = "<<AIFORGE_CTX_CONDENSED>>"
_CONDENSE_CLOSE = "<</AIFORGE_CTX_CONDENSED>>"
# E: pinned-goal markers — kept OUTSIDE the condense sentinel so a repeated
# condense strips the rolling summary but NEVER the original task.
_GOAL_PIN_OPEN = "<<AIFORGE_PINNED_GOAL>>"
_GOAL_PIN_CLOSE = "<</AIFORGE_PINNED_GOAL>>"

# WEB-lookup intent. STRONG cues explicitly ask for the open web → always force
# web_search. WEAK cues ("latest version", "release notes") also mean the web —
# BUT commonly appear in LOCAL-code questions too ("bump to the latest version in
# package.json", "what's the current version in my config"), so they only fire
# when NO local-code indicator is present. A bare URL is in neither list — a URL
# already routes to web_crawl/web_fetch.
_WEB_INTENT_STRONG_RE = re.compile(
    r"\b(search\s+(the\s+)?web|search\s+online|web\s+search|google\s+(it|for)|"
    r"look\s+(it\s+)?up\s+(online|on\s+the\s+web)|on\s+the\s+internet|"
    r"what'?s\s+new\s+in|recent\s+news|as\s+of\s+(today|now))\b",
    re.IGNORECASE)
_WEB_INTENT_WEAK_RE = re.compile(
    r"\b(latest\s+(version|release|news|stable)|current\s+version|"
    r"newest\s+version|release\s+notes|up[-\s]?to[-\s]?date)\b",
    re.IGNORECASE)
# Signals the "latest/current version" question is about THIS codebase, not the
# web — suppress the weak web cue then.
_LOCAL_CODE_CTX_RE = re.compile(
    r"(`[^`]+`|\b[\w./-]+\.(py|js|ts|tsx|jsx|java|go|rs|rb|json|ya?ml|toml|txt|md|"
    r"cfg|ini|lock|xml|gradle)\b|package\.json|requirements\.txt|pyproject|"
    r"\bmy\s+(code|repo|project|config|file|app)|\bthis\s+(repo|project|file|"
    r"codebase|code)\b|\bin\s+(the|my|this)\s+\w+)",
    re.IGNORECASE)


def _has_web_intent(text: str) -> bool:
    """True when the user's message signals a LIVE-WEB lookup and carries no URL
    (a URL already drives web_crawl). Strong cues always match; weak version/
    release cues are suppressed when the message is clearly about local code —
    so "bump to the latest version in package.json" does NOT force a web search."""
    t = (text or "").strip()
    if not t:
        return False
    if re.search(r"https?://", t):   # a URL → web_crawl path already handles it
        return False
    if _WEB_INTENT_STRONG_RE.search(t):
        return True
    if _WEB_INTENT_WEAK_RE.search(t) and not _LOCAL_CODE_CTX_RE.search(t):
        return True
    return False


_WEB_LOOKUP_DIRECTIVE = (
    "[web lookup required] The user is asking for information that must come "
    "from the LIVE web (a search, the latest/current version, release notes, "
    "or recent news). You MUST call `web_search` FIRST with a focused query, "
    "then `web_fetch` the most authoritative result to confirm, and base your "
    "answer ONLY on what you find — do NOT answer from prior knowledge, it may "
    "be out of date. If web_search returns no results, refine the query (drop "
    "years/qualifiers) and retry ONCE before saying you couldn't find it."
)


_CANCELLED = object()   # sentinel: generation abandoned because Stop was pressed

# Bound on concurrent generation threads (live + abandoned-but-still-running).
# H1 abandons a cancelled LLM call to a daemon thread; the underlying urllib
# request can't be interrupted, so it keeps a connection until it returns/times
# out (AIFORGE_LLM_TIMEOUT_S). This semaphore stops spam Stop+resend from
# stacking UNBOUNDED zombie generations: a new one waits for a slot (i.e. for a
# zombie to finish) — which matches reality on a serialized local backend. The
# wait itself is cancellable.
_GEN_SEM = None


def _gen_sem():
    global _GEN_SEM
    if _GEN_SEM is None:
        try:
            _n = max(1, int(os.environ.get("AIFORGE_CHAT_MAX_INFLIGHT_GEN", "3")))
        except ValueError:
            _n = 3
        _GEN_SEM = __import__("threading").BoundedSemaphore(_n)
    return _GEN_SEM


def _complete_cancellable(complete_fn, role, convo, session_id):
    """Run the (synchronous, uncancellable) LLM call on a side thread so a Stop
    can interrupt it. H1: previously the cancel flag was only checked between
    ReAct steps, so on a slow local model Stop appeared dead for the WHOLE
    generation (minutes). Now we poll the cancel token while the call runs and
    return the ``_CANCELLED`` sentinel the instant it's set — abandoning the
    call (it finishes in the background, daemon thread, result ignored). The
    sentinel (not ``None``) keeps a legitimately-empty completion distinct from
    a cancel. No session → call inline."""
    from aiforge_core.runtime import chat_cancel
    if session_id is None:
        return complete_fn(role, convo)
    import threading as _th
    sem = _gen_sem()
    # Acquire a generation slot (cancellable wait). At the cap, a fresh
    # generation blocks until a prior (possibly abandoned) one finishes.
    while not sem.acquire(timeout=0.2):
        if chat_cancel.is_cancelled(session_id):
            return _CANCELLED

    box: dict = {}
    ev = _th.Event()             # per-call abort signal for the client HTTP layer

    def _call():
        # Bind the cancel token on THIS thread so the LLM client's HTTP layer
        # aborts the in-flight request the instant Stop fires (true model-
        # reclaim, not just abandoning the thread). Best-effort — a stub
        # complete_fn that never reaches the client is simply unaffected.
        try:
            from aiforge_core.llm import client as _client
            _client.set_cancel_event(ev)
        except Exception:  # noqa: BLE001
            pass
        try:
            box["out"] = complete_fn(role, convo)
        except Exception as exc:  # noqa: BLE001 — surfaced on the main thread
            box["err"] = exc
        finally:
            sem.release()        # free the slot when the call REALLY finishes

    t = _th.Thread(target=_call, daemon=True)
    t.start()
    while t.is_alive():
        if chat_cancel.is_cancelled(session_id):
            ev.set()             # abort the in-flight HTTP request
            return _CANCELLED    # slot frees when the (now-aborting) request ends
        t.join(timeout=0.2)
    # The request may have been aborted just as it finished — treat any
    # post-loop cancel as a cancel, not an error.
    if chat_cancel.is_cancelled(session_id):
        return _CANCELLED
    if "err" in box:
        raise box["err"]
    return box.get("out")


# Below this resolved context window (tokens) a small local box gets lean
# ("cave") context automatically — the operator needn't flip a setting.
# Override the threshold with AIFORGE_CAVE_AUTO_WINDOW; a big-window model
# stays above it and keeps the full context.
_CAVE_AUTO_WINDOW_DEFAULT = 49152   # 48K


def _cave_auto_window() -> int:
    raw = os.environ.get("AIFORGE_CAVE_AUTO_WINDOW")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _CAVE_AUTO_WINDOW_DEFAULT


def _resolved_window(role: str | None = None) -> int:
    """The resolved context window in tokens. Routes through the ONE window
    source (``model_registry.effective_context_window``) so cave sizing, the
    seed/sys-prompt budgets and the window-scaled caps all agree (A3): prefer
    the per-role registry / auto-detected window, else the global setting."""
    try:
        from aiforge_core.config import model_registry
        return int(model_registry.effective_context_window(role))
    except Exception:  # noqa: BLE001
        try:
            from aiforge_core.config import runtime_settings
            return int(runtime_settings.get("context_window"))
        except Exception:  # noqa: BLE001
            return 131072


def _window_scaled(floor: int, frac: float, role: str | None = None) -> int:
    """A window-relative section cap: ``max(floor, window_chars × frac)``.

    The floor is today's fixed value (so a 32K window is byte-identical); on a
    bigger window the cap grows with it so a 256K box is actually used. Uses the
    SAME resolved-window source as every other budget (A3). Soft-fails to the
    floor on any error."""
    try:
        win = _resolved_window(role)
        return max(floor, int(win * 4 * frac))
    except Exception:  # noqa: BLE001
        return floor


def _cave_mode() -> bool:
    """Cave mode = leanest useful context (smaller repo map, skip optional
    skills/workflows/mentions blocks, fewer memory hits, tighter condense
    budget).

    Resolution — an EXPLICIT operator choice always wins, in order:
      1. env ``AIFORGE_CAVE_MODE`` (1/0 force on/off)
      2. an explicitly-stored ``cave_mode`` setting (UI wrote it — 1/0)
    Only when NEITHER is set do we AUTO-enable cave for a small resolved
    context window (<= ``AIFORGE_CAVE_AUTO_WINDOW``, default 48K), so a
    small local box gets lean context without the operator flipping a
    setting while a big-window model keeps the full context."""
    env = os.environ.get("AIFORGE_CAVE_MODE")
    if env is not None:
        return env not in ("0", "false", "")
    try:
        from aiforge_core.config import runtime_settings
        # Distinguish an explicitly-stored value from the unset default (a
        # stored 0 = operator opted OUT and must be respected).
        stored = runtime_settings._read_store().get("cave_mode")
        if isinstance(stored, int):
            return stored > 0
    except Exception:  # noqa: BLE001
        pass
    # Unset → auto-enable when the window is small enough that the full
    # context wouldn't fit comfortably.
    try:
        return _resolved_window() <= _cave_auto_window()
    except Exception:  # noqa: BLE001
        return False


def _compress_prompt(text: str) -> str:
    """Squeeze whitespace bloat out of the assembled prompt before it hits the
    LLM — dense context fits a small local window better and costs fewer tokens
    (the user's 'caveman'-style ask). SAFE/structural only: collapses runs of
    blank lines to one, strips trailing spaces, and drops consecutive duplicate
    lines. No words removed, no reordering — semantics unchanged. Off with
    AIFORGE_CHAT_COMPRESS_PROMPT=0."""
    if os.environ.get("AIFORGE_CHAT_COMPRESS_PROMPT", "1") in ("0", "false"):
        return text
    out: list[str] = []
    blanks = 0
    prev = None
    for raw in text.splitlines():
        ln = raw.rstrip()
        if not ln:
            blanks += 1
            if blanks > 1:
                continue          # collapse multiple blank lines to one
            out.append("")
            continue
        blanks = 0
        if ln == prev:
            continue              # drop an immediately-repeated line
        out.append(ln)
        prev = ln
    return "\n".join(out).strip()


# Measured size of the built system prompt (``_SYSTEM``) in chars — reserved
# out of the window so it isn't counted as available history.
_SYSTEM_PROMPT_CHARS = 14000
# Never let the history budget collapse to <=0 on a tiny window.
_CTX_BUDGET_FLOOR_CHARS = 4000


def _ctx_budget_chars(role: str | None = None,
                      sys_chars: int | None = None) -> int:
    """Char budget for the running conversation before auto-condensing. 0
    disables. Explicit override: AIFORGE_CHAT_CONTEXT_BUDGET_CHARS. Otherwise
    SIZED TO THE CONFIGURED MODEL WINDOW (context_window tokens → ~4 chars/token)
    MINUS the reservations that aren't available for history — the output cap
    (``max_output_tokens``) and the system prompt — so on a 32K local window the
    budget leaves real room for INPUT instead of assuming the whole window is
    history. ``sys_chars`` reserves the ACTUAL assembled system-prompt size when
    the caller knows it (M1); when omitted it falls back to the ~14K
    ``_SYSTEM_PROMPT_CHARS`` estimate. A cave/non-cave headroom fraction is then
    applied to the remaining usable space, and a floor keeps the budget positive
    on a tiny window."""
    env = os.environ.get("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    reserve_sys = _SYSTEM_PROMPT_CHARS if sys_chars is None else max(0, int(sys_chars))
    win = 0
    # Per-model context window (registry) for this role wins over the global.
    if role:
        try:
            from aiforge_core.config import model_registry
            win = int(model_registry.context_window_for_role(role))
        except Exception:  # noqa: BLE001
            win = 0
    if win <= 0:
        try:
            from aiforge_core.config import runtime_settings
            win = int(runtime_settings.get("context_window"))
        except Exception:  # noqa: BLE001
            win = 0
    # Fraction of the (post-reserve) window kept as live history before we
    # condense. This is a SAFETY margin ON TOP of the explicit output + system
    # reservations below (the 4-chars/token estimate is imprecise and a turn's
    # tool output can still grow mid-flight), so it is < 1.0 on purpose — but
    # the old 0.55 was too conservative and made a big window (e.g. a 256k model)
    # feel like ~120k. 0.85 uses most of a large window (256k → condense ~210k)
    # while still leaving a ~15% cushion for the 4-chars/token estimate error +
    # mid-turn tool growth. Cave mode still condenses sooner. Tunable per deploy.
    _default_frac = 0.40 if _cave_mode() else 0.85
    try:
        headroom = float(os.environ.get("AIFORGE_CTX_HISTORY_FRACTION", _default_frac))
        headroom = min(0.95, max(0.15, headroom))  # clamp to a sane band
    except (TypeError, ValueError):
        headroom = _default_frac
    if win > 0:
        # Reserve what the request needs beyond history: the model's own reply
        # (output cap) and the system prompt. ~4 chars/token.
        try:
            from aiforge_core.config import runtime_settings
            out_chars = int(runtime_settings.get("max_output_tokens")) * 4
        except Exception:  # noqa: BLE001
            out_chars = 4096 * 4
        usable = win * 4 - out_chars - reserve_sys
        budget = int(max(usable, _CTX_BUDGET_FLOOR_CHARS) * headroom)
        return max(budget, _CTX_BUDGET_FLOOR_CHARS)
    return 24000 if _cave_mode() else 48000


# ── system-prompt budgeting (Fix C2) ────────────────────────────────────
# convo[0] (the system message) is NEVER shrunk by _compact_convo (it only
# condenses convo[1:-keep_recent]). Every dynamic block (repo summary/map,
# skills, workflows, mentions, memory + chat recall, images) is appended to
# it, so on a small window the un-condensable system prompt alone can
# overflow. We cap the assembled system prompt to a fraction of the window,
# dropping/truncating the LOWEST-priority injected blocks first and always
# keeping the core prompt + rules.
_SYS_PROMPT_FLOOR_CHARS = 8000


def _sys_prompt_frac() -> float:
    """Fraction of the window reserved for the (un-condensable) system prompt.
    Default 0.35 (env ``AIFORGE_SYS_PROMPT_FRAC``) — co-budgeted with the Doer
    seed (also 0.35) + the output cap so seed+sysprompt+output ≤ window (A1)."""
    try:
        return float(os.environ.get("AIFORGE_SYS_PROMPT_FRAC", "0.35"))
    except (TypeError, ValueError):
        return 0.35


def _sys_prompt_budget_chars(role: str | None = None) -> int:
    """Char cap for the assembled system prompt = :func:`_sys_prompt_frac` of
    the resolved window in chars, floored (scaled down on small windows).
    Threads ``role`` so it uses the SAME per-role window as the seed + history
    budgets (A3).

    C1: co-budgeted with the Doer seed + the output reservation so
    ``seed + sysprompt + out ≤ window×4`` holds even at 4K/8K/16K, where the
    fixed 8000 floor + a full-window output reservation used to overflow. The
    output reservation is capped at a window fraction and the floor scales
    down with whatever is left."""
    try:
        win = _resolved_window(role)
    except Exception:  # noqa: BLE001
        win = 32768
    win_chars = win * 4
    sys_frac = _sys_prompt_frac()
    try:
        from aiforge_core.runtime import text_doer as _td
        from aiforge_core.config import runtime_settings
        out_tok_chars = int(runtime_settings.get("max_output_tokens")) * 4
        out_chars = _td._out_reserve_chars(win_chars, out_tok_chars)
    except Exception:  # noqa: BLE001
        out_chars = min(8192 * 4, int(win_chars * 0.4))
    sys_reserve = int(win_chars * sys_frac)
    usable = win_chars - out_chars - sys_reserve
    floor = min(_SYS_PROMPT_FLOOR_CHARS, max(0, usable) // 3)
    return max(sys_reserve, floor)


_SYS_CAP_MARK = "\n…(system prompt truncated to fit context window)\n"


def _cap_system_prompt(sys_msg: str, budget: int, *, protect: int = 0) -> str:
    """Guarantee ``len(sys_msg) <= budget`` — the backstop under the block-aware
    assembly. Preserves the first ``protect`` chars (the core prompt + rules)
    and truncates the lower-priority injected TAIL first; if even the core
    exceeds the budget it hard-truncates. No-op when already under cap or
    ``budget <= 0``. Soft: never raises."""
    try:
        if budget <= 0 or len(sys_msg) <= budget:
            return sys_msg
        # Keep the first `budget - marker` chars (the core prompt + rules sit at
        # the FRONT, so the front-preserving cut drops the injected tail first).
        keep = budget - len(_SYS_CAP_MARK)
        if keep <= 0:
            return sys_msg[:max(0, budget)]
        return sys_msg[:keep] + _SYS_CAP_MARK
    except Exception:  # noqa: BLE001
        return sys_msg


def _compact_mode() -> str:
    """'llm' = summarise the dropped middle with the model (code-aware);
    'heuristic' (default) = cheap rolling breadcrumb, no extra LLM call."""
    m = os.environ.get("AIFORGE_COMPACT_MODE", "").strip().lower()
    if m in ("llm", "heuristic"):
        return m
    try:
        from aiforge_core.config import runtime_settings
        return "llm" if int(runtime_settings.get("compact_llm")) > 0 else "heuristic"
    except Exception:  # noqa: BLE001
        return "heuristic"


_COMPACT_SYS = (
    "You compress an earlier slice of a coding-assistant conversation into a "
    "DENSE, CODE-AWARE summary the assistant can rely on after the raw turns are "
    "dropped. Preserve, concretely: files/paths touched, function/class/symbol "
    "names, decisions made + their rationale, errors hit + fixes, commands run + "
    "outcomes, and any unresolved threads or the user's standing asks. Drop "
    "pleasantries and dead ends. Output 4-12 terse bullet lines, no preamble.")


def _text_of(m: dict) -> str:
    """Text of a chat message — handles the multimodal LIST form (a vision turn
    rewrites content to ``[{type:text,...}, {image...}]``) so callers never call
    .strip() on a list (which crashed the compactor)."""
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c
                        if isinstance(p, dict) and p.get("type") == "text")
    return c if isinstance(c, str) else ""


def _condense_timeout_s() -> float:
    """Wall-clock cap for the condense summariser call (env
    ``AIFORGE_CONDENSE_TIMEOUT_S``, default 30s; <=0 disables). On the Doer
    path ``session_id is None`` so ``_complete_cancellable`` runs the LLM call
    INLINE with no timeout — a wedged endpoint would hang the whole turn on a
    condense. This bounds it so the turn falls back to the non-LLM breadcrumb."""
    try:
        return float(os.environ.get("AIFORGE_CONDENSE_TIMEOUT_S", "30"))
    except (TypeError, ValueError):
        return 30.0


def _llm_summarize_middle(middle: list[dict], complete_fn, session_id=None) -> str:
    """Code-aware LLM summary of the dropped middle. Swappable model via
    AIFORGE_COMPACT_ROLE. Routed through _complete_cancellable so a Stop can
    interrupt it (and it honours the generation cap). Bounded by a wall-clock
    timeout (:func:`_condense_timeout_s`) so a hung endpoint can't wedge the
    turn. Returns '' on any failure / cancel / timeout so the caller falls
    back to the heuristic breadcrumb."""
    if complete_fn is None or not middle:
        return ""
    transcript = []
    for m in middle:
        r = (m.get("role") or "").upper()
        c = _text_of(m).strip()
        if c:
            transcript.append(f"{r}: {c}")
    body = "\n".join(transcript)
    if len(body) > 24000:        # bound the summariser's own input
        body = body[:12000] + "\n…\n" + body[-12000:]
    sum_role = os.environ.get("AIFORGE_COMPACT_ROLE", "").strip() or "doer"
    msgs = [{"role": "system", "content": _COMPACT_SYS},
            {"role": "user", "content": "Summarise this slice:\n\n" + body}]
    timeout = _condense_timeout_s()

    def _call() -> str:
        try:
            out = _complete_cancellable(complete_fn, sum_role, msgs, session_id)
            if out is _CANCELLED or not isinstance(out, str):
                return ""
            return out.strip()
        except Exception:  # noqa: BLE001
            return ""

    if timeout <= 0:
        return _call()
    import threading as _th
    box: dict = {}

    def _worker() -> None:
        box["out"] = _call()

    t = _th.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        # Summariser is wedged — abandon it (daemon) and fall back to the
        # cheap non-LLM condense so the turn proceeds.
        return ""
    return box.get("out", "")


def _compact_convo(convo: list[dict], *, keep_recent: int = 18, role: str | None = None,
                   complete_fn=None, session_id=None) -> list[dict]:
    """Auto-condense a long chat history so the context can't overflow.

    Keeps the system message + the last ``keep_recent`` turns verbatim and
    collapses everything in between into ONE breadcrumb note (count of omitted
    messages + the tools used so far). Structural only — no extra LLM call, so
    it's cheap and runs every turn. The agent can re-read files / ask the user
    if it needs detail from before the condense point."""
    # M1: reserve the ACTUAL system-prompt size (convo[0]) rather than the fixed
    # 14K estimate, and DON'T re-count it in the over-budget sum below (it's
    # reserved, not history) — the old code both subtracted a constant AND
    # summed the real system chars = a double-count.
    sys_chars = (len(_text_of(convo[0]))
                 if convo and convo[0].get("role") == "system" else 0)
    budget = _ctx_budget_chars(role, sys_chars=sys_chars)
    if budget <= 0:
        return convo
    # Scale the verbatim tail to the budget: on a SMALL window, keeping 18 turns
    # could itself exceed the budget (condense fires but can't get under it).
    # ~2k chars/turn heuristic, floor 4 so there's always a usable recent slice.
    keep_recent = max(4, min(keep_recent, budget // 2000))
    if len(convo) <= keep_recent + 2:
        return convo
    if sum(len(_text_of(m)) for m in convo[1:]) <= budget:
        return convo
    tail = convo[-keep_recent:]
    middle = convo[1:-keep_recent]
    if not middle:
        return convo
    tools: list[str] = []
    user_asks: list[str] = []
    finals: list[str] = []
    for m in middle:
        mrole = m.get("role")
        content = _text_of(m).strip()
        if mrole == "assistant":
            mt = _ACTION_RE.search(content)
            if mt:
                tools.append(mt.group(1))
            # An assistant FINAL (no ACTION:) is a substantive outcome — keep a
            # short trace so the summary carries decisions, not just tool counts.
            elif content and "ACTION:" not in content:
                finals.append(content.replace("\n", " ")[:160])
        elif mrole == "user" and content and not content.startswith("OBSERVATION:"):
            user_asks.append(content.replace("\n", " ")[:120])
    import collections as _c
    used = ", ".join(f"{t}×{n}" for t, n in _c.Counter(tools).most_common(8)) \
        or "discussion + reads"
    # ROLLING summary: carry forward asks/outcomes from the PRIOR breadcrumb
    # (if any) and merge with this window's, so a second+ condense doesn't drop
    # the original thread. Capped slices ([-N:]) keep it bounded — no growth.
    prior = convo[0].get("content") or ""
    prior_block = re.search(
        re.escape(_CONDENSE_OPEN) + r"(.*?)" + re.escape(_CONDENSE_CLOSE),
        prior, flags=re.S)
    if prior_block:
        ptext = prior_block.group(1)
        pa = re.search(r"Earlier asks: (.+)", ptext)
        po = re.search(r"Earlier outcomes: (.+)", ptext)
        if pa:
            user_asks = [s.strip() for s in pa.group(1).split(" · ")] + user_asks
        if po:
            finals = [s.strip() for s in po.group(1).split(" · ")] + finals
    # Rolling SUMMARY of the dropped middle — earlier asks + outcomes, not just
    # tool counts — so condensation doesn't erase what was discussed/decided
    # (the agent stops "forgetting" the thread after a long session). Heuristic,
    # no extra LLM call.
    summary_bits: list[str] = []
    if user_asks:
        summary_bits.append("Earlier asks: " + " · ".join(user_asks[-6:]))
    if finals:
        summary_bits.append("Earlier outcomes: " + " · ".join(finals[-4:]))
    summary = ("\n" + "\n".join(summary_bits)) if summary_bits else ""
    # Optional CODE-AWARE LLM summary of the dropped middle (swappable model via
    # AIFORGE_COMPACT_ROLE). Falls back to the heuristic breadcrumb on failure.
    llm_summary = ""
    if _compact_mode() == "llm":
        llm_summary = _llm_summarize_middle(middle, complete_fn, session_id)
    # Wrap the breadcrumb in a unique sentinel so the next condense can strip
    # exactly THIS block (not a look-alike phrase a rule/skill might contain).
    if llm_summary:
        # Append the structured asks/outcomes tail so the NEXT condense's parser
        # can still carry the thread forward (without it, repeated condenses in
        # LLM mode silently dropped everything before the prior summary).
        note = (f"{_CONDENSE_OPEN}\n"
                f"[earlier conversation auto-condensed — {len(middle)} messages "
                f"omitted. Summary of what happened:\n{llm_summary}\n{summary}\n"
                "Re-read a file or ask the user if you need more detail.]\n"
                f"{_CONDENSE_CLOSE}")
    else:
        note = (f"{_CONDENSE_OPEN}\n"
                "[earlier conversation auto-condensed to fit the context window — "
                f"{len(middle)} messages omitted. Work done so far: {used}.{summary}\n"
                "Re-read a file or ask the user if you need detail from before "
                f"this point.]\n{_CONDENSE_CLOSE}")
    # Fold the breadcrumb INTO the system message rather than inserting a
    # separate 'user' turn — that avoids two consecutive same-role messages
    # (some providers reject those) and keeps the tail's alternation intact.
    # Strip any prior sentinel block first so the system message can't grow
    # unbounded across repeated condenses.
    sys_text = re.sub(
        re.escape(_CONDENSE_OPEN) + r".*?" + re.escape(_CONDENSE_CLOSE),
        "", convo[0].get("content") or "", flags=re.S).rstrip()
    # E: pin the ORIGINAL task into the system prompt (ONCE) so a long, repeatedly
    # -condensed run never loses WHAT it's building — the first user turn gets
    # summarised out of the middle, and on later condenses it's gone entirely, so
    # capture it here the first time and keep it OUTSIDE the strippable condense
    # sentinel. Small-window models otherwise drift off-goal mid-task.
    if _GOAL_PIN_OPEN not in sys_text:
        _goal = next((_text_of(m).strip() for m in convo[1:]
                      if m.get("role") == "user" and _text_of(m).strip()
                      and not _text_of(m).strip().startswith("OBSERVATION:")), "")
        _goal = _goal.split("\n\n---\n[Interpreted request")[0].strip() or _goal
        if _goal:
            sys_text = (sys_text + "\n\n" + _GOAL_PIN_OPEN + "\nORIGINAL TASK (stay "
                        "on this until it's fully done + verified):\n"
                        + _goal[:1200] + "\n" + _GOAL_PIN_CLOSE).strip()
    head = [{"role": "system", "content": (sys_text + "\n\n" + note).strip()}]
    return head + tail


def _ctx_on(block: str) -> bool:
    """Is the dynamic-context ``block`` injected this turn? Operator knob —
    ``ctx_no_{block}`` (runtime setting / env) = 1 turns it off. Default ON.
    Blocks: recall · mentions · skills · workflows · repomap · summary."""
    try:
        from aiforge_core.config import runtime_settings
        return int(runtime_settings.get(f"ctx_no_{block}")) == 0
    except Exception:  # noqa: BLE001
        return True


def _repomap_max_chars() -> int:
    """Char cap for the repo-map block. An explicit ``AIFORGE_REPOMAP_MAX_CHARS``
    wins verbatim (0 disables); otherwise window-relative (A2): floor 6000,
    growing at ~2% of the window."""
    env = os.environ.get("AIFORGE_REPOMAP_MAX_CHARS")
    if env is not None:
        try:
            return max(0, int(env))
        except (TypeError, ValueError):
            pass
    return _window_scaled(6000, 0.02)


_SYM_PATTERNS = {
    ".py": r"^\s*(?:async\s+)?(?:class|def)\s+(\w+)",
    ".java": r"^\s*(?:@\w+\s*)*(?:public|private|protected|static|final|abstract|\s)*"
             r"(?:class|interface|enum|record)\s+(\w+)"
             r"|^\s*(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\],\s.]+?\s+(\w+)\s*\(",
    ".go": r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)|^\s*type\s+(\w+)\s",
    ".ts": r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|const|enum)\s+(\w+)",
    ".tsx": r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|const)\s+(\w+)",
    ".js": r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|const)\s+(\w+)",
    ".rb": r"^\s*(?:class|module|def)\s+([\w.]+)",
    ".rs": r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait|impl)\s+(\w+)",
    ".c": r"^\s*[\w\*\s]+?\s+(\w+)\s*\([^;]*\)\s*\{",
    ".cpp": r"^\s*(?:class|struct)\s+(\w+)|^\s*[\w:<>\*&\s]+?\s+(\w+)\s*\([^;]*\)\s*\{",
    ".cs": r"^\s*(?:public|private|protected|internal|static|\s)*(?:class|interface|struct|enum)\s+(\w+)",
    ".kt": r"^\s*(?:fun|class|interface|object)\s+(\w+)",
    ".php": r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|function)\s+(\w+)",
}


def _build_symbol_map(cwd: str, max_files: int = 200, max_syms: int = 12) -> str:
    """A lightweight, dependency-free repo map: each source file → its top-level
    symbols (classes/functions/methods) via regex. Fast (no tree-sitter/aider),
    language-agnostic, so the agent navigates by SYMBOLS not blind `find`."""
    import re as _re
    base = str(_workspace_root() or cwd)
    compiled = {ext: _re.compile(pat, _re.MULTILINE)
                for ext, pat in _SYM_PATTERNS.items()}
    rows: list[tuple[str, list[str]]] = []
    seen = 0
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            if ext not in compiled:
                continue
            if seen >= max_files:
                return _fmt_symbol_rows(base, rows, truncated=True)
            fp = os.path.join(root, f)
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    src = fh.read(200_000)
            except Exception:  # noqa: BLE001
                continue
            syms: list[str] = []
            for m in compiled[ext].finditer(src):
                nm = next((g for g in m.groups() if g), None)
                if nm and nm not in syms and nm not in ("if", "for", "while",
                                                        "switch", "catch", "return"):
                    syms.append(nm)
                if len(syms) >= max_syms:
                    break
            if syms:
                rows.append((os.path.relpath(fp, base), syms))
                seen += 1
    return _fmt_symbol_rows(base, rows, truncated=False)


def _fmt_symbol_rows(base: str, rows: list, truncated: bool) -> str:
    if not rows:
        return ""
    cap = _repomap_max_chars()
    out: list[str] = []
    total = 0
    for rel, syms in rows:
        line = f"{rel}: {', '.join(syms)}"
        if cap and total + len(line) > cap:
            truncated = True
            break
        out.append(line)
        total += len(line) + 1
    body = "\n".join(out)
    tail = "\n… (more — grep/find for the rest)" if truncated else ""
    return body + tail


def _build_repo_map(cwd: str, max_entries: int = 160, max_depth: int = 3) -> str:
    """Repo map for the system prompt so the agent navigates by SYMBOLS, not blind
    `find`. Prefers the tree-sitter + PageRank Aider RepoMap (ranked functions/
    classes per file) — critical on big repos where a bare file tree is useless;
    falls back to a compact directory tree. Best-effort, char-capped."""
    base = str(_workspace_root() or cwd)
    if not os.path.isdir(base):
        return f"WORKING DIRECTORY: {base} (not a directory)"
    # 1. Tree-sitter Aider RepoMap — ranked symbols (the good map for analysis).
    #    TIME-BOUNDED: the first parse of a big repo can be slow, so run it in a
    #    thread with a short budget (AIFORGE_REPOMAP_BUDGET_S, default 6s). If it
    #    doesn't finish in time, fall through to the instant dir tree — the cached
    #    Aider map then serves later turns. Never blocks the turn.
    if os.environ.get("AIFORGE_CHAT_AIDER_MAP", "1") not in ("0", "false"):
        try:
            budget = float(os.environ.get("AIFORGE_REPOMAP_BUDGET_S", "30"))
        except ValueError:
            budget = 6.0
        _out: dict = {}

        def _work():
            try:
                from aiforge_core.memory.code_context import aider_digest
                _out["d"] = aider_digest(base, [])
            except Exception:  # noqa: BLE001
                _out["d"] = ""
        import threading as _th
        _t = _th.Thread(target=_work, daemon=True)
        _t.start()
        _t.join(budget)
        digest = _out.get("d") or ""
        if digest.strip():
            cap = _repomap_max_chars()
            if cap and len(digest) > cap:
                digest = digest[:cap] + "\n… (truncated — grep/find/list_dir for more)"
            return ("REPO MAP (ranked symbols via tree-sitter — the key functions/"
                    "classes per file; navigate by these, don't blind-`find`):\n"
                    f"WORKING DIRECTORY: {base}\n{digest}")
        # else: aider absent / timed out / empty → lightweight regex symbol map.

    # 2. Lightweight regex symbol map (no deps, fast) — file → its symbols.
    if os.environ.get("AIFORGE_CHAT_SYMBOL_MAP", "1") not in ("0", "false"):
        try:
            symmap = _build_symbol_map(base)
            if symmap and symmap.strip():
                return ("REPO MAP (each file → its top-level classes/functions; "
                        "navigate by these symbols, don't blind-`find`):\n"
                        f"WORKING DIRECTORY: {base}\n{symmap}")
        except Exception:  # noqa: BLE001
            pass
    lines: list[str] = []
    base_depth = base.rstrip(os.sep).count(os.sep)
    try:
        for root, dirs, files in os.walk(base):
            depth = root.rstrip(os.sep).count(os.sep) - base_depth
            if depth >= max_depth:
                dirs[:] = []
                continue
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS
                             and not d.startswith("."))
            rel = os.path.relpath(root, base)
            indent = "" if rel == "." else "  " * depth
            if rel != ".":
                lines.append(f"{indent}{os.path.basename(root)}/")
            for f in sorted(files)[:40]:
                if not f.startswith("."):
                    lines.append(f"{indent}  {f}")
            if len(lines) >= max_entries:
                lines.append("  … (truncated — use find/grep/list_dir for more)")
                break
    except Exception:  # noqa: BLE001
        pass
    tree = "\n".join(lines) or "(empty)"
    # Char cap — the line/depth caps above bound entries but a wide tree can
    # still be huge. Window-relative (A2): floor 6000 on a 32K window, grows
    # with a bigger window so a 256K box shows a fuller map.
    cap = _repomap_max_chars()
    if cap and len(tree) > cap:
        tree = tree[:cap] + "\n… (truncated to fit context — use find/grep/list_dir)"
    return ("REPO MAP of the working directory (already known — do NOT "
            f"re-list directories you can see here):\nWORKING DIRECTORY: {base}\n"
            f"{tree}")


def _repo_name(cwd: str) -> str:
    # Canonical resolver (git-toplevel) — was workspace-dir basename, which
    # drifted from the recall key. Delegates now.
    from aiforge_core.runtime import repo_ident as _ri
    return _ri.repo_name(cwd, sentinel="repo")


# Keyword → tool-scope tag: which tool a request is likely to use. A recalled
# learning tagged ``tool:<name>`` (see the learner guidance) gets a score bump
# in recall so the working JQL/filter/config the agent figured out LAST time
# resurfaces for the same TYPE of request — instead of re-deriving it.
_TOOL_TAG_HINTS = {
    "tool:jira": ("jira", "jql", "issue", "ticket", "sprint", "epic"),
    "tool:confluence": ("confluence", "wiki", "space", "page"),
    "tool:git": ("git", "branch", "commit", "rebase", "pull request", " pr ", "merge"),
    "tool:email": ("email", "smtp", "inbox", "mailbox"),
    "tool:gitlab": ("gitlab", "merge request", " mr "),
}


def _tool_tags(query: str) -> list[str]:
    q = f" {(query or '').lower()} "
    return [tag for tag, kws in _TOOL_TAG_HINTS.items()
            if any(k in q for k in kws)]


_ASK_LEAD_RE = re.compile(
    r"^(?:also|and|plus|then|next|additionally|why|how|what|when|where|which|"
    r"who|can|could|should|would|is|are|does|do|did|will|fix|add|make|check|"
    r"recheck|verify|update|create|remove|delete|use|show|explain|list|"
    r"implement|write|run|test|deploy|review|rename|refactor|change|ensure)\b",
    re.IGNORECASE)


def _split_asks(text: str, cap: int = 8) -> list[str]:
    """Break the user's CURRENT message into its distinct asks so a
    multi-part message ("fix X. also why does Y happen? and add Z") gets a
    CHECKLIST instead of the model answering part 1 and stopping — simple
    mode has no enhancer/spec, so nothing else tracks the parts. Heuristic
    and conservative: bullets/numbered lines count as-is; otherwise sentence
    segments that look like a question or an imperative. Returns [] (no
    checklist) when only one ask is found."""
    t = (text or "").strip()
    if len(t) < 25:
        return []
    parts: list[str] = []
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    bullets = [re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", ln) for ln in lines
               if re.match(r"^(?:[-*•]|\d+[.)])\s+", ln)]
    if len(bullets) >= 2:
        parts = bullets
    else:
        # sentence segmentation + " also "/" and then " connectors
        segs: list[str] = []
        for chunk in re.split(r"(?<=[?.!;])\s+|\n+", t):
            segs.extend(re.split(
                r"\s+(?=(?:also|and then|and also|plus|additionally)\b)",
                chunk, flags=re.IGNORECASE))
        for s in segs:
            s = s.strip(" .")
            if len(s) < 12:
                continue
            if s.endswith("?") or _ASK_LEAD_RE.match(s):
                parts.append(s)
    parts = [p[:160] for p in parts if p.strip()][:cap]
    return parts if len(parts) >= 2 else []


def _memory_recall(cwd: str, query: str, limit: int = 6,
                   session_id: "int | None" = None) -> str:
    """Proactive memory recall at SESSION START — pull prior decisions /
    gotchas / learnings relevant to the user's opening request so the agent
    arrives informed (self-learning) instead of re-deriving what past
    sessions already worked out. Best-effort: never breaks the turn."""
    q = (query or "").strip()
    if not q:
        return ""
    hits: list[dict] = []
    try:
        from aiforge_core.memory import unified_query as _uq
        # F2/M3: recall under the SAME repo the chat WRITE path files facts
        # under (git-toplevel basename), else sqlite_memory.recall filters
        # them out (WHERE repo=?). M4: exclude the current live session so
        # this turn's own messages don't return as "prior chat".
        _repo = _chat_repo_key(cwd)
        res = _uq.query(q, limit=limit, repo=_repo,
                        exclude_session=session_id,
                        boost_tags=_tool_tags(q))
        if isinstance(res, dict):
            hits = res.get("hits", []) or []
    except Exception:  # noqa: BLE001
        hits = []
    if not hits:
        return ""
    _preamble = ("RELEVANT MEMORY recalled for this request (prior decisions / "
                 "gotchas / learnings from earlier sessions — consult before "
                 "re-deriving):\n")
    # Map→summarize: many scattered hits → ONE compact briefing (LLM). Empty
    # (disabled / too few / model down) falls back to the raw ranked list.
    try:
        from aiforge_core.memory import recall_summary
        brief = recall_summary.summarize_hits(q, hits)
    except Exception:  # noqa: BLE001
        brief = ""
    if brief:
        return _preamble + brief
    lines: list[str] = []
    for h in hits:
        txt = (h.get("text") or "").strip().replace("\n", " ")
        if not txt:
            continue
        src = h.get("source") or ""
        lines.append(f"- {txt[:240]}" + (f"  ({src})" if src else ""))
        if len(lines) >= limit:
            break
    if not lines:
        return ""
    return _preamble + "\n".join(lines)


def _chat_session_recall(query: str, session_id: "int | None",
                         limit: int = 4, drop_session: "int | None" = None) -> str:
    """Proactive recall from PRIOR CHAT SESSIONS — surface things the user
    discussed in OTHER conversations that may bear on this request, so simple
    chat has continuity across sessions (not just within one). Cheap + local
    (one SQLite scan). Best-effort: never breaks the turn."""
    q = (query or "").strip()
    if not q:
        return ""
    try:
        from aiforge_core.runtime import chat_store
        hits = chat_store.search_messages(q, limit=limit + 2,
                                          exclude_session=session_id)
    except Exception:  # noqa: BLE001
        hits = []
    lines: list[str] = []
    for h in hits:
        # the immediate-prior session is already injected as prev-session — skip
        # its hits here so it doesn't double-surface (older sessions still show).
        if drop_session is not None and h.get("session_id") == drop_session:
            continue
        content = (h.get("content") or "").strip().replace("\n", " ")
        if not content:
            continue
        if len(lines) >= limit:
            break
        title = h.get("session_title") or "chat"
        role = h.get("role") or "user"
        lines.append(f"- [{title}] {role}: {content}")
    if not lines:
        return ""
    return ("RELEVANT PRIOR CHAT SESSIONS — things you discussed with the user "
            "in OTHER conversations that may bear on this request (cite them if "
            "you use them):\n" + "\n".join(lines))


def _repo_context(cwd: str) -> str:
    """The persistent PROJECT SUMMARY for this repo — what it is + what's
    been done — injected every turn so follow-ups have continuity. Read
    from the per-repo memory file (source=repo:<name>); if none exists yet,
    auto-build a starter from the detected stack + README so there's always
    something. The summary is updated at the end of each session run."""
    base = str(_workspace_root() or cwd)
    repo = _repo_name(cwd)
    try:
        from aiforge_core.memory import md_store
        p = _cached_find_by_source(f"repo:{repo}")
        if p is not None:
            body = md_store._parse(p).get("body", "")
            if body.strip():
                return (f"PROJECT SUMMARY — {repo} (what this repo is + what "
                        f"prior sessions did):\n{body[:1800]}")
    except Exception:  # noqa: BLE001
        pass
    # Starter (first time): stack + README excerpt.
    stacks: list[str] = []
    try:
        from aiforge_core.runtime.tools.project_runner import detect
        stacks = detect(base).get("stacks", [])
    except Exception:  # noqa: BLE001
        pass
    readme = ""
    for rn in ("README.md", "Readme.md", "readme.md", "README.rst", "README.txt"):
        rp = os.path.join(base, rn)
        if os.path.isfile(rp):
            try:
                readme = open(rp, encoding="utf-8", errors="ignore").read()[:700]
            except Exception:  # noqa: BLE001
                pass
            break
    out = f"PROJECT SUMMARY — {repo} (auto-detected; refine as you learn):\n"
    out += f"- Stack(s): {', '.join(stacks) or 'unknown'}\n"
    if readme:
        out += f"- README excerpt:\n{readme}\n"
    return out


def _fire_stop(reason: str, cwd: str) -> None:
    """Best-effort Stop lifecycle hook at a terminal loop exit. Soft-fail: a
    hooks error must never break the turn's clean shutdown."""
    try:
        from aiforge_core.runtime import hooks as _hooks
        _hooks.fire("Stop", {"reason": reason}, cwd)
    except Exception:  # noqa: BLE001
        pass


_EDIT_TOOL_NAMES = frozenset((
    "write_file", "file_write", "edit", "editor", "edit_block", "file_patch",
    "patch", "apply_patch", "str_replace", "create_file",
))


def _verify_on_final_enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_VERIFY_ON_FINAL", "1") not in ("0", "false")


def _verify_max_rounds() -> int:
    try:
        return max(0, min(12, int(os.environ.get("AIFORGE_CHAT_VERIFY_ROUNDS", "6"))))
    except ValueError:
        return 6


def _run_project_verify(cwd: str):
    """Run the project's test/build gate on ``cwd``. Returns ``(ok, output)`` or
    ``(None, "")`` when there's nothing to test — reuses the SAME runner the
    pipeline reconcile uses, so simple/doer runs get the same real pass/fail."""
    try:
        from aiforge_core.runtime.parallel_subtasks import _project_test_output
        return _project_test_output(cwd)
    except Exception:  # noqa: BLE001
        return None, ""


def _post_edit_syntax_error(name: str, args: dict, cwd: str) -> "str | None":
    """Syntax-check the file an edit tool just wrote. Returns an error string
    when broken, else None. Reuses the pipeline's language-agnostic syntax_guard
    (Python compile, js/java/go/… checkers, brace-balance fallback)."""
    path = None
    for k in ("path", "file", "filename", "file_path", "target"):
        v = (args or {}).get(k)
        if isinstance(v, str) and v:
            path = v
            break
    if not path:
        return None
    abs_path = path if os.path.isabs(path) else os.path.join(cwd, path)
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except Exception:  # noqa: BLE001
        return None
    try:
        from aiforge_core.runtime.syntax_guard import validate_syntax
        ok, err = validate_syntax(path, content)
        return None if ok else err
    except Exception:  # noqa: BLE001
        return None


def _verify_fix_message(output: str) -> str:
    """A directed 'tests are failing, fix them' turn — reuses the reconcile's
    root-cause hint engine so the local model gets concrete guidance, not just
    the raw traceback."""
    try:
        from aiforge_core.runtime.parallel_subtasks import _directed_hints
        hints = _directed_hints(output or "")
    except Exception:  # noqa: BLE001
        hints = []
    hint_block = ("\n\nDIRECTED FIXES:\n" + "\n".join(f"- {h}" for h in hints)) if hints else ""
    return (
        "[automated verification — not the user] You said you were done, but the "
        "project's tests do NOT pass. Do NOT reply FINAL until they do. Read the "
        "failures below, fix the IMPLEMENTATION (not the tests, unless a test "
        "clearly contradicts the request), then re-run the tests to confirm.\n\n"
        "TEST OUTPUT:\n```\n" + (output or "")[-3000:] + "\n```" + hint_block)


