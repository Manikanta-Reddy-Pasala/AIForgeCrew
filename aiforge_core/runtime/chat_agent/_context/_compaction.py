from __future__ import annotations

import os
import re

from .._shell import _ACTION_RE
from ._claim_guard import _claims_file_edits
from ._generation import _CANCELLED, _complete_cancellable
from ._window import _ctx_budget_chars


_CONDENSE_OPEN = "<<AIFORGE_CTX_CONDENSED>>"
_CONDENSE_CLOSE = "<</AIFORGE_CTX_CONDENSED>>"
# E: pinned-goal markers — kept OUTSIDE the condense sentinel so a repeated
# condense strips the rolling summary but NEVER the original task.
_GOAL_PIN_OPEN = "<<AIFORGE_PINNED_GOAL>>"
_GOAL_PIN_CLOSE = "<</AIFORGE_PINNED_GOAL>>"


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


def _recent_tail_count(convo: list[dict], budget: int, *,
                       ceiling: int = 18, floor: int = 4,
                       frac: float = 0.5) -> int:
    """How many TRAILING messages to keep verbatim on a condense — capped by
    SIZE, not a fixed count. Walks from the newest message accumulating chars
    until the kept tail would exceed ``frac`` of the history budget (or the
    ``ceiling`` count), always keeping at least ``floor``. A fixed count kept N
    large tool-outputs verbatim and barely freed the window; sizing by chars
    guarantees condense lands near ``frac`` of budget even when recent turns are
    big. Env ``AIFORGE_CONDENSE_TAIL_FRACTION`` overrides ``frac``."""
    if budget <= 0:
        return floor
    try:
        frac = float(os.environ.get("AIFORGE_CONDENSE_TAIL_FRACTION", frac))
    except (TypeError, ValueError):
        pass
    frac = min(0.9, max(0.1, frac))
    cap = int(budget * frac)
    kept = total = 0
    for m in reversed(convo[1:]):          # newest → oldest, skip system
        ln = len(_text_of(m))
        if kept >= floor and (total + ln > cap or kept >= ceiling):
            break
        total += ln
        kept += 1
    return max(floor, min(kept, ceiling))


def _middle_signals(middle: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """``(tools, user_asks, finals)`` distilled from the dropped middle.

    An assistant FINAL (no ACTION:) is a substantive outcome — a short trace is
    kept so the summary carries decisions, not just tool counts. BUT an unbacked
    EDIT CLAIM ("I applied the fix to X") is never folded in: with no ACTION it
    was a hallucinated write, and persisting it as an "Earlier outcome" makes
    the model believe the edit happened forever after — the bug compounds as the
    session grows.
    """
    tools: list[str] = []
    user_asks: list[str] = []
    finals: list[str] = []
    for m in middle:
        content = _text_of(m).strip()
        role = m.get("role")
        if role == "assistant":
            mt = _ACTION_RE.search(content)
            if mt:
                tools.append(mt.group(1))
            elif (content and "ACTION:" not in content
                    and not _claims_file_edits(content)):
                finals.append(content.replace("\n", " ")[:160])
        elif role == "user" and content and not content.startswith("OBSERVATION:"):
            user_asks.append(content.replace("\n", " ")[:120])
    return tools, user_asks, finals


def _carry_prior_thread(prior: str, user_asks: list, finals: list) -> tuple[list, list]:
    """ROLLING summary: carry forward asks/outcomes from the PRIOR breadcrumb so
    a second+ condense doesn't drop the original thread."""
    block = re.search(re.escape(_CONDENSE_OPEN) + r"(.*?)"
                      + re.escape(_CONDENSE_CLOSE), prior or "", flags=re.S)
    if not block:
        return user_asks, finals
    text = block.group(1)
    pa = re.search(r"Earlier asks: (.+)", text)
    po = re.search(r"Earlier outcomes: (.+)", text)
    if pa:
        user_asks = [s.strip() for s in pa.group(1).split(" · ")] + user_asks
    if po:
        finals = [s.strip() for s in po.group(1).split(" · ")] + finals
    return user_asks, finals


def _summary_tail(user_asks: list, finals: list) -> str:
    """Earlier asks + outcomes, not just tool counts — so condensation doesn't
    erase what was discussed/decided (the agent stops "forgetting" the thread
    after a long session). Heuristic, no extra LLM call; capped slices keep it
    bounded."""
    bits: list[str] = []
    if user_asks:
        bits.append("Earlier asks: " + " · ".join(user_asks[-6:]))
    if finals:
        bits.append("Earlier outcomes: " + " · ".join(finals[-4:]))
    return ("\n" + "\n".join(bits)) if bits else ""


def _breadcrumb(middle: list, used: str, summary: str, llm_summary: str) -> str:
    """The condense note, wrapped in a unique sentinel so the NEXT condense can
    strip exactly THIS block (not a look-alike phrase a rule/skill contains).

    The LLM form appends the structured asks/outcomes tail too, so the next
    condense's parser can still carry the thread forward — without it, repeated
    condenses in LLM mode silently dropped everything before the prior summary.
    """
    if llm_summary:
        body = (f"[earlier conversation auto-condensed — {len(middle)} messages "
                f"omitted. Summary of what happened:\n{llm_summary}\n{summary}\n"
                "Re-read a file or ask the user if you need more detail.]")
    else:
        body = ("[earlier conversation auto-condensed to fit the context window "
                f"— {len(middle)} messages omitted. Work done so far: {used}."
                f"{summary}\nRe-read a file or ask the user if you need detail "
                "from before this point.]")
    return f"{_CONDENSE_OPEN}\n{body}\n{_CONDENSE_CLOSE}"


def _pin_goal(sys_text: str, convo: list[dict]) -> str:
    """Pin the ORIGINAL task into the system prompt ONCE, OUTSIDE the strippable
    condense sentinel — so a long, repeatedly-condensed run never loses WHAT it
    is building. The first user turn gets summarised out of the middle, and on
    later condenses it is gone entirely; small-window models otherwise drift
    off-goal mid-task."""
    if _GOAL_PIN_OPEN in sys_text:
        return sys_text
    goal = next((_text_of(m).strip() for m in convo[1:]
                 if m.get("role") == "user" and _text_of(m).strip()
                 and not _text_of(m).strip().startswith("OBSERVATION:")), "")
    goal = goal.split("\n\n---\n[Interpreted request")[0].strip() or goal
    if not goal:
        return sys_text
    return (sys_text + "\n\n" + _GOAL_PIN_OPEN + "\nORIGINAL TASK (stay on this "
            "until it's fully done + verified):\n" + goal[:1200] + "\n"
            + _GOAL_PIN_CLOSE).strip()


def _stripped_system(convo: list[dict]) -> str:
    """The system message without any prior sentinel block, so it can't grow
    unbounded across repeated condenses."""
    return re.sub(re.escape(_CONDENSE_OPEN) + r".*?" + re.escape(_CONDENSE_CLOSE),
                  "", convo[0].get("content") or "", flags=re.S).rstrip()


def _compact_convo(convo: list[dict], *, keep_recent: int = 18, role: str | None = None,
                   complete_fn=None, session_id=None, force: bool = False) -> list[dict]:
    """Auto-condense a long chat history so the context can't overflow.

    Keeps the system message + the last ``keep_recent`` turns verbatim and
    collapses everything in between into ONE breadcrumb note (count of omitted
    messages + the tools used so far). Structural only — no extra LLM call, so
    it's cheap and runs every turn. ``force=True`` condenses regardless of the
    budget (the caller wants a fresh window, not just a safe one). The agent can
    re-read files / ask the user if it needs detail from before the condense
    point."""
    # M1: reserve the ACTUAL system-prompt size (convo[0]) rather than the fixed
    # 14K estimate, and DON'T re-count it in the over-budget sum below (it's
    # reserved, not history) — the old code both subtracted a constant AND
    # summed the real system chars = a double-count.
    sys_chars = (len(_text_of(convo[0]))
                 if convo and convo[0].get("role") == "system" else 0)
    budget = _ctx_budget_chars(role, sys_chars=sys_chars)
    if budget <= 0:
        return convo
    # Size-aware tail: keep the newest messages up to ~half the budget (by
    # CHARS), floor 4 — so condense lands ~50% of budget even when recent turns
    # are large (a fixed count kept N huge tool-outputs verbatim and barely freed
    # the window). ``keep_recent`` is the ceiling.
    keep_recent = _recent_tail_count(convo, budget, ceiling=keep_recent)
    if len(convo) <= keep_recent + 2:
        return convo
    # ``force`` condenses even when the history still FITS — used when the loop
    # grants a runaway-cap extension: the next slice of work should start from a
    # summary, not from thousands of accumulated turns that merely happened to
    # be under budget.
    if not force and sum(len(_text_of(m)) for m in convo[1:]) <= budget:
        return convo
    middle = convo[1:-keep_recent]
    if not middle:
        return convo

    import collections as _c
    tools, user_asks, finals = _middle_signals(middle)
    user_asks, finals = _carry_prior_thread(convo[0].get("content") or "",
                                            user_asks, finals)
    used = (", ".join(f"{t}×{n}" for t, n in _c.Counter(tools).most_common(8))
            or "discussion + reads")
    # Optional CODE-AWARE LLM summary of the dropped middle (swappable model via
    # AIFORGE_COMPACT_ROLE). Falls back to the heuristic breadcrumb on failure.
    llm_summary = (_llm_summarize_middle(middle, complete_fn, session_id)
                   if _compact_mode() == "llm" else "")
    note = _breadcrumb(middle, used, _summary_tail(user_asks, finals),
                       llm_summary)
    # Fold the breadcrumb INTO the system message rather than inserting a
    # separate 'user' turn — that avoids two consecutive same-role messages
    # (some providers reject those) and keeps the tail's alternation intact.
    sys_text = _pin_goal(_stripped_system(convo), convo)
    head = [{"role": "system", "content": (sys_text + "\n\n" + note).strip()}]
    return head + convo[-keep_recent:]
