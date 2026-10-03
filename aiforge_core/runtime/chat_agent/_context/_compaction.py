from __future__ import annotations

import os
import re

from .._shell import _ACTION_RE
from . import _note, _structured, _summary_bg, _tail_cut
from .._guards.file_edit import _claims_file_edits
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


# The model summary sits inside the breadcrumb between these, so a later
# splice or condense can find exactly that text again.
_SUM_OPEN = "Summary of what happened:\n"
_SUM_CLOSE = "\n(end of summary)"
_SUM_RE = re.compile(re.escape(_SUM_OPEN) + r"(.*?)" + re.escape(_SUM_CLOSE), re.S)
_GEN_RE = re.compile(r"\(condense #(\d+)\)")
_BLOCK_RE = re.compile(re.escape(_CONDENSE_OPEN) + r"(.*?)"
                       + re.escape(_CONDENSE_CLOSE), re.S)
#: A summary longer than this is cut: it must stay smaller than the history
#: it replaces.
_SUMMARY_MAX_CHARS = 4000


def _prior_block(sys_text: str) -> str:
    m = _BLOCK_RE.search(sys_text or "")
    return m.group(1) if m else ""


def _block_gen(block: str) -> int:
    m = _GEN_RE.search(block or "")
    return int(m.group(1)) if m else 0


def _block_summary(block: str) -> str:
    m = _SUM_RE.search(block or "")
    return m.group(1).strip() if m else ""


def _summary_messages(middle, prior: str = "") -> list:
    """The prompt for a background summary. Bounded so the call cannot be
    larger than the history it is replacing. ``prior`` is the summary the
    breadcrumb already carries: the new one must cover it too, or each
    condense would forget everything before the last slice."""
    transcript = []
    for m in middle:
        r = (m.get("role") or "").upper()
        c = _text_of(m).strip()
        if c:
            transcript.append(f"{r}: {c}")
    body = "\n".join(transcript)
    if len(body) > 24000:
        body = body[:12000] + "\n…\n" + body[-12000:]
    if prior:
        body = "EARLIER SUMMARY (keep what still matters):\n" + prior + \
            "\n\nNEWER TURNS:\n" + body
    if _structured.enabled():
        return [{"role": "system", "content": _structured.SYSTEM},
                {"role": "user", "content":
                 "Fill in the JSON record for this slice:\n\n" + body}]
    return [{"role": "system", "content": _COMPACT_SYS},
            {"role": "user", "content": "Summarise this slice:\n\n" + body}]


def _text_complete(complete_fn, role: str, msgs: list):
    """A plain text completion.

    The turn's native complete_fn owns that turn's tool queue. Calling it
    from this thread would drop the agent's batched reads or overwrite them
    with the summarizer's. A function without ``take_queued`` is a test
    double and is safe to call. Everything else goes through the client,
    and never through the chat generation slot.
    """
    if complete_fn is not None and not hasattr(complete_fn, "take_queued"):
        return complete_fn(role, msgs)
    from aiforge_core.llm import client
    return client.complete(role, msgs)


def _summary_role() -> str:
    # learner, not doer: the call counts against compaction_rpm, not the
    # chat bucket. AIFORGE_COMPACT_ROLE picks another role.
    return os.environ.get("AIFORGE_COMPACT_ROLE", "").strip() or "learner"


def _schedule_llm_summary(middle, complete_fn, run_key, gen: int,
                          prior: str = "") -> None:
    """Start the model summary of condense ``gen`` and return at once.

    The turn already has the heuristic breadcrumb, so it must not wait on
    this call. The result is spliced into THIS breadcrumb when it lands
    (:func:`_splice_ready_summary`). No run key, no summary: the key is what
    keeps parallel runs from reading each other's result.
    """
    if not run_key or _compact_mode() != "llm" or not middle:
        return
    msgs = _summary_messages(list(middle), prior)
    role = _summary_role()
    # A reply that holds no typed record is stored as "": the breadcrumb stands.
    _summary_bg.schedule(
        run_key, gen,
        lambda: _structured.summarise(_text_complete(complete_fn, role, msgs)))


def _clean_summary(text: str) -> str:
    """The model's text, bounded and unable to fake the note's own markers
    (a later splice or carry would otherwise cut it at the wrong place)."""
    text = (text or "").replace(_SUM_CLOSE.strip(), "(end)")
    text = re.sub(r"(?m)^(Earlier (?:asks|outcomes)):", r"\1 -", text)
    text = text.replace(_CONDENSE_OPEN, "").replace(_CONDENSE_CLOSE, "")
    return text.strip()[:_SUMMARY_MAX_CHARS]


def _with_summary(block: str, summary: str) -> str:
    summary = _clean_summary(summary)
    part = _SUM_OPEN + summary + _SUM_CLOSE
    if _SUM_RE.search(block):
        return _SUM_RE.sub(lambda _m: part, block, count=1)
    cut = [i for i in (block.find("\nEarlier asks:"),
                       block.find("\nEarlier outcomes:"),
                       block.find("\nRe-read a file")) if i >= 0]
    at = min(cut) if cut else len(block)
    return block[:at] + "\n" + part + block[at:]


def _splice_ready_summary(convo: list[dict], run_key) -> list[dict]:
    """Put a finished model summary into the breadcrumb it was written for.

    The breadcrumb is in the condense note (the message after the system
    prompt) or, with ``AIFORGE_STABLE_PREFIX=0``, at the end of the system
    message. Checked every step, so the summary lands as soon as it is ready
    instead of one condense later. A summary for any other condense is
    discarded."""
    if not run_key or not convo or convo[0].get("role") != "system":
        return convo
    at = _note.note_index(convo) or 0
    text = convo[at].get("content")
    if not isinstance(text, str):
        return convo
    block = _prior_block(text)
    if not block:
        return convo
    summary = _summary_bg.take(run_key, _block_gen(block))
    if not summary:
        return convo
    new = text.replace(block, _with_summary(block, summary), 1)
    return convo[:at] + [{**convo[at], "content": new}] + convo[at + 1:]


def release_run(run_key) -> None:
    """The run ended: drop its summary, finished or not."""
    _summary_bg.release(run_key)


def _tail_fraction(frac: float = 0.5) -> float:
    """Share of the history budget a condense keeps verbatim."""
    try:
        frac = float(os.environ.get("AIFORGE_CONDENSE_TAIL_FRACTION", frac))
    except (TypeError, ValueError):
        pass
    return min(0.9, max(0.1, frac))


def _system_chars(convo: list[dict]) -> int:
    return (len(_text_of(convo[0]))
            if convo and convo[0].get("role") == "system" else 0)


def _tail_budget_chars(convo: list[dict], role: str | None = None) -> int:
    """Characters of recent history a condense keeps verbatim (0 = no limit)."""
    budget = _ctx_budget_chars(role, sys_chars=_system_chars(convo))
    return int(budget * _tail_fraction()) if budget > 0 else 0


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
    cap = int(budget * _tail_fraction(frac))
    kept = total = 0
    for m in reversed(convo[1:]):          # newest → oldest, skip system
        ln = len(_text_of(m))
        if kept >= floor and (total + ln > cap or kept >= ceiling):
            break
        total += ln
        kept += 1
    return max(floor, min(kept, ceiling))


_HARNESS_NOTE = re.compile(
    r"^(OBSERVATION:|" + re.escape(_note.NOTE_OPEN) + r"|\[(?:[^\]]*not the user|system reminder)[^\]]*\]"
    r"|You (?:narrated|signalled|described) )")


def _is_harness_note(content: str) -> bool:
    """A user-role message the loop wrote (a tool result or a nudge), not the
    user's own words — it must not crowd out the real asks."""
    return bool(_HARNESS_NOTE.match(content))


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
        elif role == "user" and content and not _is_harness_note(content):
            user_asks.append(content.replace("\n", " ")[:120])
    return tools, user_asks, finals


_ERR_LINE = re.compile(
    r"(?:Traceback|Error\b|Exception\b|FAILED|\berror:|AssertionError)", re.I)
_FACT_CAP = 8


def _middle_facts(middle: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """``(edited, read, errors)`` from the dropped middle, taken verbatim from the
    calls and results (no model): the paths a tool wrote or read, and the first
    error line of a failing result. A tally of tool names loses exactly these."""
    import json as _json

    from aiforge_core.runtime.tools.mutating import FILE_WRITE_TOOLS

    from .._shell import _ARGS_RE
    edited: list[str] = []
    read: list[str] = []
    errors: list[str] = []
    for m in middle:
        content = _text_of(m)
        if m.get("role") == "assistant":
            mt = _ACTION_RE.search(content)
            ma = _ARGS_RE.search(content)
            if not (mt and ma):
                continue
            try:
                args = _json.loads(ma.group(1))
            except ValueError:
                continue
            if not isinstance(args, dict):
                continue
            paths = [args.get("path")] + list(args.get("paths") or [])
            paths += [e.get("path") for e in args.get("edits") or []
                      if isinstance(e, dict)]
            bucket = edited if mt.group(1).lower() in FILE_WRITE_TOOLS else read
            bucket.extend(str(x) for x in paths if isinstance(x, str) and x)
        elif m.get("role") == "user" and content.startswith("OBSERVATION:"):
            for line in content.splitlines()[:80]:
                if _ERR_LINE.search(line) and len(line.strip()) > 8:
                    errors.append(line.strip()[:160])
                    break
    return (list(dict.fromkeys(edited)), list(dict.fromkeys(read)),
            list(dict.fromkeys(errors)))


def _carry_prior_facts(prior: str) -> tuple[list[str], list[str], list[str]]:
    """The ``(edited, read, errors)`` lists a PRIOR condense wrote into its note,
    so a second condense keeps them instead of starting over."""
    block = re.search(re.escape(_CONDENSE_OPEN) + r"(.*?)"
                      + re.escape(_CONDENSE_CLOSE), prior or "", flags=re.S)
    text = _SUM_RE.sub("", block.group(1)) if block else ""
    out = []
    for label in ("Files edited", "Files read", "Errors seen"):
        m = re.search(label + r": (.+)", text)
        out.append([x.strip() for x in m.group(1).split(" · ")] if m else [])
    return out[0], out[1], out[2]


def _carry_prior_thread(prior: str, user_asks: list, finals: list) -> tuple[list, list]:
    """ROLLING summary: carry forward asks/outcomes from the PRIOR breadcrumb so
    a second+ condense doesn't drop the original thread."""
    block = re.search(re.escape(_CONDENSE_OPEN) + r"(.*?)"
                      + re.escape(_CONDENSE_CLOSE), prior or "", flags=re.S)
    if not block:
        return user_asks, finals
    text = _SUM_RE.sub("", block.group(1))    # the model summary is not asks
    pa = re.search(r"Earlier asks: (.+)", text)
    po = re.search(r"Earlier outcomes: (.+)", text)
    if pa:
        user_asks = [s.strip() for s in pa.group(1).split(" · ")] + user_asks
    if po:
        finals = [s.strip() for s in po.group(1).split(" · ")] + finals
    return user_asks, finals


def _summary_tail(user_asks: list, finals: list,
                  facts: "tuple[list, list, list] | None" = None) -> str:
    """Earlier asks + outcomes, not just tool counts — so condensation doesn't
    erase what was discussed/decided (the agent stops "forgetting" the thread
    after a long session). Heuristic, no extra LLM call; capped slices keep it
    bounded."""
    bits: list[str] = []
    if user_asks:
        bits.append("Earlier asks: " + " · ".join(user_asks[-6:]))
    if finals:
        bits.append("Earlier outcomes: " + " · ".join(finals[-4:]))
    edited, read, errors = facts or ([], [], [])
    if edited:
        bits.append("Files edited: " + " · ".join(edited[-_FACT_CAP:]))
    if read:
        bits.append("Files read: " + " · ".join(read[-_FACT_CAP:]))
    if errors:
        bits.append("Errors seen: " + " · ".join(errors[-5:]))
    return ("\n" + "\n".join(bits)) if bits else ""


def _breadcrumb(middle: list, used: str, summary: str, llm_summary: str,
               gen: int = 1, saved: "str | None" = None) -> str:
    """The condense note, wrapped in a unique sentinel so the NEXT condense can
    strip exactly THIS block (not a look-alike phrase a rule/skill contains).

    ``gen`` numbers the condense; a model summary is only ever spliced into
    the note with its own number. "Work done so far" is kept in both forms:
    the model summary may lag this slice, the tool tally never does. The
    asks/outcomes tail lets the next condense carry the thread forward.
    """
    llm = (f"\n{_SUM_OPEN}{_clean_summary(llm_summary)}{_SUM_CLOSE}"
           if llm_summary else "")
    if saved:
        more = ("The full text of those messages is saved: call memory_lookup "
                f'{{"id": "{saved}"}} to read it (a page at a time), or re-read '
                "a file.")
    else:
        more = "Re-read a file or ask the user if you need detail from before this point."
    body = ("[earlier conversation auto-condensed to fit the context window "
            f"(condense #{gen}) — {len(middle)} messages omitted. Work done so "
            f"far: {used}.{llm}{summary}\n{more}]")
    return f"{_CONDENSE_OPEN}\n{body}\n{_CONDENSE_CLOSE}"


def _pin_goal(sys_text: str, convo: list[dict], pin: "str | None" = None) -> str:
    """Pin the ORIGINAL task into the system prompt ONCE, OUTSIDE the strippable
    condense sentinel — so a long, repeatedly-condensed run never loses WHAT it
    is building. The first user turn gets summarised out of the middle, and on
    later condenses it is gone entirely; small-window models otherwise drift
    off-goal mid-task.

    ``pin`` is the loop's own text for the block (this turn's task, later
    instructions, files changed); it replaces the previous one each time."""
    if pin is not None:
        sys_text = re.sub(r"\s*" + re.escape(_GOAL_PIN_OPEN) + r".*?"
                          + re.escape(_GOAL_PIN_CLOSE), "", sys_text, flags=re.S)
        return (sys_text + "\n\n" + _GOAL_PIN_OPEN + "\n" + pin + "\n"
                + _GOAL_PIN_CLOSE).strip()
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
    return re.sub(r"\s*" + re.escape(_CONDENSE_OPEN) + r".*?" + re.escape(_CONDENSE_CLOSE),
                  "", convo[0].get("content") or "", flags=re.S).rstrip()


def _hist_chars(msgs: list[dict]) -> int:
    return sum(len(_text_of(m)) for m in msgs)


_GOAL_RE = re.compile(re.escape(_GOAL_PIN_OPEN) + r".*?"
                      + re.escape(_GOAL_PIN_CLOSE), re.S)


def _board_re():
    from .._turn._tasks import _BOARD_RE
    return _BOARD_RE


def _restart_on() -> bool:
    """``AIFORGE_COMPACT_RESTART=0`` keeps a rolling breadcrumb on every
    condense instead of restarting from a handoff on the second one."""
    return os.environ.get("AIFORGE_COMPACT_RESTART", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _clean_system(msg: dict) -> dict:
    """The system message without any pinned goal, task board or condense
    block (an older layout put them there). The SAME object when it has none,
    so a run that never used that layout keeps byte 0 untouched."""
    text = msg.get("content")
    if not isinstance(text, str) or not (
            _CONDENSE_OPEN in text or _GOAL_PIN_OPEN in text
            or "<<AIFORGE_TASK_BOARD>>" in text):
        return msg
    text = _board_re().sub("", _GOAL_RE.sub("", text))
    text = re.sub(r"\s*" + re.escape(_CONDENSE_OPEN) + r".*?"
                  + re.escape(_CONDENSE_CLOSE), "", text, flags=re.S).rstrip()
    return {**msg, "content": text}


def _restart_block(st, middle, tail_text: str, carried: str, gen: int,
                   saved, goal_block: str) -> "str | None":
    """The condense block for a RESTART: the typed handoff record (goal, done,
    files, what failed, last error, next) built from the run's state, plus the
    rolling asks/files/errors, in place of one more stacked breadcrumb. None
    on any failure, and the caller writes the ordinary breadcrumb."""
    try:
        from aiforge_core.runtime import handoff
        h = handoff.build_chat(st)
        if not h["goal"] and goal_block:
            inner = goal_block[len(_GOAL_PIN_OPEN):-len(_GOAL_PIN_CLOSE)]
            h["goal"] = " ".join(inner.split())[:1200]
        head = (f"[HANDOFF (condense #{gen}) — the history was condensed "
                f"{gen - 1} time(s) already, so it restarts from this record "
                f"instead of stacking another summary; {len(middle)} older "
                "messages omitted. Do not repeat what failed.]")
        body = handoff.render(h, saved, header=head)
        block = f"{_CONDENSE_OPEN}\n{body}{tail_text}\n{_CONDENSE_CLOSE}"
        return _with_summary(block, carried) if carried else block
    except Exception:  # noqa: BLE001 — a failed restart is the usual breadcrumb
        return None


def _compact_convo(convo: list[dict], *, keep_recent: int = 18, role: str | None = None,
                   complete_fn=None, session_id=None, force: bool = False,
                   keep_min: int = 0, pin: "str | None" = None,
                   run_key: "str | None" = None,
                   handoff_st=None) -> list[dict]:
    """Auto-condense a long chat history so the context can't overflow.

    Keeps the system message + the last ``keep_recent`` turns verbatim and
    collapses everything in between into ONE breadcrumb note (count of omitted
    messages + the tools used so far). Structural only — the model summary
    (``compact_llm``) is written behind the turn and spliced in when it lands,
    keyed by ``run_key``; without one there is no model summary. ``force=True``
    condenses regardless of the budget (the caller wants a fresh window, not
    just a safe one). ``keep_min`` trailing messages are always kept: tool
    results the model has not read yet must not be summarised away.
    ``session_id`` is accepted for callers and unused: a session is not a run.

    The system message is never rewritten: the breadcrumb, the pinned task and
    the task board go in a note message right after it (see :mod:`_note`), so
    a local server's prompt cache survives a condense. A history that was
    already condensed once restarts from a handoff built from ``handoff_st``
    (the run's state) instead of stacking a second breadcrumb."""
    convo = _splice_ready_summary(convo, run_key)
    plen = _note.prefix_len(convo)
    note_msgs = convo[1:plen]
    work = [convo[0]] + convo[plen:] if plen > 1 else convo
    # M1: reserve the ACTUAL system-prompt size (convo[0]) rather than the fixed
    # 14K estimate, and DON'T re-count it in the over-budget sum below (it's
    # reserved, not history). The note counts as history.
    budget = _ctx_budget_chars(role, sys_chars=_system_chars(convo))
    if budget <= 0:
        return convo
    # Size-aware tail: keep the newest messages up to ~half the budget (by
    # CHARS), floor 4. ``keep_recent`` is the ceiling. Unread results are kept
    # only while they fit: past the budget, keeping them would just fail the
    # model call.
    if keep_min and _hist_chars(convo[-keep_min:]) > budget:
        keep_min = 0
    keep_recent = max(_recent_tail_count(work, budget, ceiling=keep_recent),
                      keep_min)
    if len(work) <= keep_recent + 2:
        return convo
    # ``force`` condenses even when the history still FITS — used when the loop
    # grants a runaway-cap extension.
    if not force and _hist_chars(convo[1:]) <= budget:
        return convo
    room = max(0, budget - _hist_chars(work[-keep_recent:])
               - _hist_chars(note_msgs))
    start, needs_opener = _tail_cut.tail_start(work, keep_recent, room or 1)
    middle = work[1:start]
    if not middle:
        return convo

    import collections as _c
    stable = _note.enabled()
    sys_text = convo[0].get("content")
    sys_text = sys_text if isinstance(sys_text, str) else ""
    prior_src = _note.text(convo) or sys_text
    prior_block = _prior_block(prior_src)
    tools, user_asks, finals = _middle_signals(middle)
    user_asks, finals = _carry_prior_thread(prior_src, user_asks, finals)
    used = (", ".join(f"{t}×{n}" for t, n in _c.Counter(tools).most_common(8))
            or "discussion + reads")
    # The model summary rolls: the note keeps the last one until the summary
    # of THIS condense (which covers it) is spliced in.
    prior_gen = _block_gen(prior_block)
    gen = prior_gen + 1
    carried = _block_summary(prior_block)
    _schedule_llm_summary(middle, complete_fn, run_key, gen, carried)
    ed0, rd0, er0 = _carry_prior_facts(prior_src)
    ed1, rd1, er1 = _middle_facts(middle)
    facts = (list(dict.fromkeys(ed0 + ed1)), list(dict.fromkeys(rd0 + rd1)),
             list(dict.fromkeys(er0 + er1)))
    from aiforge_core.runtime import context_offload
    saved = context_offload.save(context_offload.render(middle))
    tail_text = _summary_tail(user_asks, finals, facts)
    tail = work[start:]
    if not stable:
        note = _breadcrumb(middle, used, tail_text, carried, gen, saved)
        # Legacy layout: fold the breadcrumb INTO the system message rather
        # than inserting a separate 'user' turn (two same-role messages).
        sys_new = _pin_goal(_stripped_system(convo), work, pin)
        head = [{"role": "system", "content": (sys_new + "\n\n" + note).strip()}]
        if needs_opener:
            tail = [_tail_cut.opener()] + tail
        kept = head + tail
    else:
        goal_prior = _GOAL_RE.search(prior_src) or _GOAL_RE.search(sys_text)
        goal_block = _pin_goal(goal_prior.group(0) if goal_prior else "",
                               work, pin)
        board_prior = (_board_re().search(prior_src)
                       or _board_re().search(sys_text))
        board_block = board_prior.group(0).strip() if board_prior else ""
        block = None
        if handoff_st is not None and prior_gen >= 1 and _restart_on():
            block = _restart_block(handoff_st, middle, tail_text, carried, gen,
                                   saved, goal_block)
        if block is None:
            block = _breadcrumb(middle, used, tail_text, carried, gen, saved)
        head = [_clean_system(convo[0]),
                _note.build(goal_block, board_block, block)]
        # [system, user note, assistant ack, user ...]: roles keep alternating.
        # A tail that opens on an assistant turn follows the note directly.
        if not needs_opener:
            head.append(_note.ack())
        kept = head + tail
    # A pointer is only honest while the body it names is still in the kept
    # system message or the tail. Compaction just dropped the middle, so put
    # that body back if the pointer would otherwise dangle — but only while it
    # fits, or the next step condenses again at once.
    from aiforge_core.runtime.context_seen import restore_dangling
    # (A pointer in the system message whose body only the dropped middle held
    # is restored there: an honest prompt beats a cache hit, and it is rare.)
    restored = restore_dangling(kept, middle)
    over = budget - _hist_chars(kept[1:]) - (_system_chars(kept)
                                             - _system_chars(convo))
    return _tail_cut.within_budget(kept, restored, max(0, over))
