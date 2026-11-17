"""A stuck run changes approach and keeps going; it does not stop to ask.

Every guard that used to pause the turn ("I keep trying the same step… could
you clarify?") calls :func:`escalate` instead. Each trip does two things: it
condenses the history (a loop is usually a model that lost the thread in a long
prompt), and it sends a stronger, different instruction than the trip before.
The task finishes. The turn ends only when the model answers, the user presses
Stop, or — after ``AIFORGE_CHAT_STUCK_ESCALATIONS`` trips (default 20, 0 =
never) — with a written summary of what is done and what remains.

``AIFORGE_CHAT_PAUSE_ON_STUCK=1`` restores the old pause-and-ask.
"""
from __future__ import annotations

import os

from aiforge_core.runtime.stuck_policy import Policy

from ._stuck.state import reset


def arm_reasoning(st) -> None:
    """A stuck step is the one that needs thinking: turn reasoning on for the
    next few model calls (see llm.reasoning.boost)."""
    st.reason_boost = max(getattr(st, "reason_boost", 0), Policy.load().reason_steps)


def pause_on_stuck() -> bool:
    return Policy.load().pause_on_stuck


_TIERS = (
    "Step back. In one line say what the last attempts had in common, then "
    "choose an approach you have NOT tried (a different tool, a different "
    "file, a smaller step) and do it now.",
    "Make the smallest change that moves the task forward and do it: read "
    "the one file you need, or write the one edit you are sure of. If a tool "
    "keeps failing, use a different tool for the same goal.",
    "Take the simplest path to finishing. Do what you can with what you "
    "already know, then verify it. If one part is truly blocked, finish the "
    "rest and say plainly what is blocked and why.",
)

_WRAP_UP = ("Stop retrying. Write `FINAL:` now: what is done, what remains, and "
            "what you would do next. Be specific.")


_SAME = ("same failure", "without progress", "no progress")
_ASIDE_RX = r"(?:^|/)(?:tests?|__tests__|spec|specs)/|(?:^|/)(?:test_[^/]*|[^/]*_test\.[a-z]+|[^/]*Tests?\.[a-zA-Z]+|[^/]*\.(?:test|spec)\.[a-z]+)$"


def _edited_sources(st) -> list:
    """Non-test source files the work changed (git, against its base)."""
    import re
    import subprocess
    cwd = str(getattr(st, "cwd", "") or "")
    if not cwd:
        return []
    try:
        from ._contract_gate import work_base
        base = work_base(st) or "HEAD"
        out = []
        for args in (["diff", "--name-only", base], ["ls-files", "--others", "--exclude-standard"]):
            r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                out += [x for x in r.stdout.splitlines() if x.strip()]
    except Exception:  # noqa: BLE001
        return []
    code = re.compile(r"\.(?:java|kt|scala|py|js|jsx|ts|tsx|go|rs|rb|cs|cpp|cc|c|h|hpp|php|swift)$")
    return sorted({f for f in out if code.search(f) and not re.search(_ASIDE_RX, f)})


def _wrong_place_hint(st, why: str) -> str:
    """Once per turn, on a same-failure trip after source edits: the fixes may
    be in the wrong place. A real job described a wrong total; every fix went
    into one later adjustment step while the wrong number was made where the
    amounts are summed, and the same check stayed red through five trips."""
    if not hasattr(st, "__dict__") or st.__dict__.get("wrong_place_hinted"):
        return ""
    if not any(k in (why or "").lower() for k in _SAME):
        return ""
    files = _edited_sources(st)
    if not files:
        return ""
    st.wrong_place_hinted = True
    shown = ", ".join(files[:4]) + (" …" if len(files) > 4 else "")
    return ("Your fixes so far are all in " + shown + ", and the result did not change: "
            "the wrong value is probably produced somewhere else. Trace it back before "
            "editing again: take the value the failing check reads, search the code for "
            "every place that computes, sets or adds to it, list those places, and fix "
            "the one on the path the request describes (that may mean undoing your "
            "earlier edits).")


def _contract_gaps(st) -> list:
    """What the request spells out (names, signatures, files) that the code
    still lacks — the turn is about to give up; a real run gave up with a
    constructor that did not match the one the request stated."""
    try:
        from ._contract_gate import _gaps
        return [str(g) for g in (_gaps(st, cached=False) or [])]
    except Exception:  # noqa: BLE001
        return []


def _own_test_hint(st, why: str) -> str:
    """Once per turn: the repeated failure is in a test file this turn created.
    A real run rewrote its own mock-heavy test about fifty times (generic
    mocking of a client builder chain did not compile) and never got back
    to the feature."""
    import re
    if not hasattr(st, "__dict__") or st.__dict__.get("own_test_hinted"):
        return ""
    if not any(k in (why or "").lower() for k in _SAME):
        return ""
    new_tests = [f for f in _new_files(st) if re.search(_ASIDE_RX, f)]
    hit = [f for f in new_tests if os.path.basename(f).rsplit(".", 1)[0] in (why or "")]
    if not hit:
        return ""
    st.own_test_hinted = True
    return ("The failure is in the test you wrote (" + ", ".join(hit[:3]) + "), not in the "
            "feature. Do not keep rewriting it: make it the simplest test that checks the "
            "behaviour (fewer mocks — a tiny real input, a local stub server, or a plain "
            "call and a check on the result), or cut it down to the cases that work. The "
            "feature code is what the request needs.")


def _new_files(st) -> list:
    import subprocess
    cwd = str(getattr(st, "cwd", "") or "")
    try:
        r = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=cwd,
                           capture_output=True, text=True, timeout=5)
        return [x for x in r.stdout.splitlines() if x.strip()] if r.returncode == 0 else []
    except Exception:  # noqa: BLE001
        return []


def _condense_on_trip() -> bool:
    """Condense the history on a stuck trip. tk-toml11 pr1a (11-13): five
    trips, each condense dropped reads the model then asked for again, which
    were refused as repeats and counted as the next trip.
    Off switch: ``AIFORGE_CHAT_STUCK_CONDENSE=0`` (core mode)."""
    import os
    return os.environ.get("AIFORGE_CHAT_STUCK_CONDENSE", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def escalate(st, why: str):
    """One stuck trip. Yields a status line and queues the nudge (and a
    condense) on ``st.convo``; returns ``"continue"`` to keep the run going, or
    ``"wrap_up"`` once the limit is spent and the model has been told to
    summarise (the caller then ends the turn if it still does not)."""
    n = getattr(st, "stuck_escalations", 0) + 1
    st.stuck_escalations = n
    arm_reasoning(st)
    pol = Policy.load()
    limit = pol.stuck_escalations
    wrapping = bool(limit) and n > limit
    if wrapping:
        st.wrapping_up = True         # "what is done, what remains" is the answer now
    _remember_failure(st, why)
    reset(st, "escalation")
    restarted = False
    if n % 2 == 0 and pol.stuck_restart:
        restarted = restart_with_handoff(st)
    elif _condense_on_trip():
        # every trip shrinks the context when the restart is off (its
        # default): the long repeating history is what keeps the loop going
        _condense(st)
    text = (_WRAP_UP if wrapping else _TIERS[min(n, len(_TIERS)) - 1])
    if not wrapping:
        hint = _wrong_place_hint(st, why) or _own_test_hint(st, why)
        if hint:
            text = text + " " + hint
    else:
        gaps = _contract_gaps(st)
        if gaps:
            text = ("Before you stop: the request names these and the code does not have "
                    "them yet — " + "; ".join(gaps[:6]) + ". Make the feature code match "
                    "them exactly now (leave a failing test of your own aside), then " +
                    text[0].lower() + text[1:])
    note = (f"[loop guard — not the user] {why} {text}" if not restarted
            else text)
    last = st.convo[-1] if st.convo else None
    if (isinstance(last, dict) and last.get("role") == "user"
            and isinstance(last.get("content"), str)):
        # Two user turns in a row break some providers: ride on the last one.
        st.convo[-1] = {**last, "content": last["content"] + "\n\n" + note}
    else:
        st.convo.append({"role": "user", "content": note})
    yield {"type": "thought", "role": "system",
           "text": f"↺ {why.rstrip('.')} — changing approach (try {n}), "
                   "the task continues"}
    return "wrap_up" if wrapping and n > limit + 1 else "continue"


def _remember_failure(st, why: str) -> None:
    """Write down the approach that just failed, so the next attempt (and the
    handoff after a restart) can say what not to repeat."""
    from aiforge_core.runtime import handoff
    attempt = handoff.last_attempt(getattr(st, "convo", []))
    handoff.record_failed(st, f"{attempt}" if attempt else why.rstrip("."))


def restart_with_handoff(st) -> bool:
    """Start the run over from a fresh context: the system prompt with the goal
    and the task board pinned, and ONE message holding the handoff. The failed
    transcript is saved (``memory_lookup {id}`` restores any detail), not kept.
    Returns True when the context was replaced."""
    try:
        from aiforge_core.runtime import context_offload, handoff
        from .._context import _compaction as C
        from .._context import _note
        from ._tasks import pin_board
        old = list(st.convo)
        if len(old) < 3 or old[0].get("role") != "system":
            return False
        oid = context_offload.save(context_offload.render(old[1:]))
        if not oid:
            return False          # never drop the transcript without a saved copy
        h = handoff.build_chat(st)
        if not h["goal"]:
            from ._convo import strip_turn_note
            h["goal"] = next((strip_turn_note(C._text_of(m)).strip()[:1200]
                              for m in old[1:]
                              if m.get("role") == "user"
                              and not C._text_of(m).strip().startswith("OBSERVATION:")
                              and not C._is_harness_note(C._text_of(m).strip())), "")
        from ._tasks import turn_pin
        pin = turn_pin(st)
        if pin:
            h = handoff.without_pinned(h)
        if _note.enabled():
            _restart_into_note(st, old, h, oid, pin)
        else:
            base = C._stripped_system(old)
            sys_text = C._pin_goal(C.strip_pinned_blocks(base) if pin else base,
                                   old, pin)
            st.convo[:] = [{**old[0], "content": sys_text},
                           {"role": "user", "content": handoff.render(h, oid)}]
        if getattr(st, "board", None):
            pin_board(st.convo, st.board)
        try:
            from ._limits import _repin_turn_note
            _repin_turn_note(st)   # the message's rules, skills and repo notes
        except Exception:  # noqa: BLE001
            pass
        seen = getattr(st, "read_sigs_seen", None)
        if hasattr(seen, "clear"):
            seen.clear()
        reset(st, "restart")
        st.restarts = getattr(st, "restarts", 0) + 1
        rec = getattr(st, "handoff_rec", None)
        if rec is not None:
            rec.note_restart(h, oid)       # the restart itself survives a crash
        return True
    except Exception:  # noqa: BLE001 — a failed restart falls back to the nudge
        return False


def _restart_into_note(st, old: list, h: dict, oid, pin=None) -> None:
    """The restarted context with the system message UNTOUCHED: the pinned
    goal, the board and the handoff go in the note right after it (see
    ``_context._note``), so the model server's prompt cache is not thrown away
    by the restart."""
    from aiforge_core.runtime import handoff
    from .._context import _compaction as C
    from .._context import _note
    sys_text = old[0].get("content")
    sys_text = sys_text if isinstance(sys_text, str) else ""
    prior_src = _note.text(old) or sys_text
    gen = C._block_gen(C._prior_block(prior_src)) + 1
    work = [old[0]] + old[_note.prefix_len(old):]
    prior_goal = C._GOAL_RE.search(prior_src) or C._GOAL_RE.search(sys_text)
    goal_block = C._pin_goal(prior_goal.group(0) if prior_goal else "", work, pin)
    head = (f"[HANDOFF (condense #{gen}) — the earlier attempt went in circles, "
            "so this is a fresh start. It holds what is known; do not repeat "
            "what failed.]")
    block = (f"{C._CONDENSE_OPEN}\n{handoff.render(h, oid, header=head)}\n"
             f"{C._CONDENSE_CLOSE}")
    st.convo[:] = [C._clean_system(old[0]), _note.build(goal_block, "", block)]


def _condense(st) -> None:
    try:
        from .._context import _compact_convo
        from ._limits import _after_condense
        from ._tasks import turn_pin
        before = len(st.convo)
        unread = (len(st.convo) - st.batch_mark
                  if getattr(st, "batch_unread", False) else 0)
        new = _compact_convo(st.convo, role=st.role, force=True, keep_recent=8,
                             keep_min=unread, pin=turn_pin(st),
                             run_key=getattr(st, "compact_key", None),
                             handoff_st=st)
        if new is not st.convo and len(new) < len(st.convo):
            st.convo[:] = new
            try:
                _after_condense(st, unread, before)
            except Exception:  # noqa: BLE001 — the condense itself stands
                pass
    except Exception:  # noqa: BLE001 — a failed condense still nudges
        pass


def give_up_message(st) -> str:
    return ("(stopped after repeated attempts: I could not move this forward. "
            "Say \"continue\" and I will pick it up from where the work is on "
            "disk, or tell me what to change.)")
