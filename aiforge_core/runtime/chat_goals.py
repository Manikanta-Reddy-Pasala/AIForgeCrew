"""Every goal the user set in a chat, and how each new message changes them.

A chat had one goal at a time: the turn's request was the newest message, the
steers typed while it ran were kept only as "later instructions", and the next
message became the request on its own. "Also add a dark mode" sent while the
agent built a login page, or "use postgres instead" after a Stop, read as the
whole task: the login page, the earlier target, was dropped.

This module keeps the chat's goals outside the history and, for every new
message (a new turn or a steer), asks the model ONE short question: given the
goals so far and this message, what are the goals now, which of them is this
turn working on, and the whole of it as one statement. A goal is only dropped
when the user's words drop it, and when the message cannot be placed (does
"use the other one" replace the target or add one?) the model gives a question
and the agent asks the user before acting.

Block shown every turn and pinned in every condense: "GOALS IN THIS CHAT".
A turn that ends with an answer (not stopped, not waiting on the user, no task
left open) closes the goals it worked on.

Store: ``<chat scratch>/session-<id>/.aiforge/goals.json`` (a temporary chat
keeps it in memory only). Off switch: ``AIFORGE_CHAT_GOALS=0``. Soft-fail
everywhere: a broken store or a failed model call never breaks a turn.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("aiforge.chat_goals")

MARK = "GOALS IN THIS CHAT"
#: Goals kept in the store (the oldest closed ones go first).
MAX_GOALS = 12
#: Closed goals still shown in the block, for "like before" / "the same".
_SHOWN_DONE = 3
_GOAL_CHARS = 1500
_UPDATE_CHARS = 600
_MAX_UPDATES = 8
_CURRENT_CHARS = 2500
_BLOCK_CHARS = 5000

_LOCK = threading.Lock()
_MEM: dict = {}          # temporary chats: session id -> goals
#: One lock per chat, held across the model call: a steer and the next turn's
#: goal pass (or a turn's close) must not overwrite each other's change.
_SESSION_LOCKS: dict = {}
#: Chats whose current turn ran the goal pass: only those turns close goals.
_PASSED: set = set()
#: Chats whose turn just ended after a goal pass: its unread steers may
#: still be kept (runtime/chat_goals.keep_unread), once.
_ENDED_PASSED: set = set()


def _session_lock(session_id) -> threading.RLock:
    with _LOCK:
        return _SESSION_LOCKS.setdefault(str(session_id), threading.RLock())

OPEN, DONE, DROPPED = "open", "done", "dropped"

_SYSTEM = (
    "You keep track of the goals a user has set for an AI coding agent in "
    "one chat. You get the goals so far (id, status, text, later changes), "
    "the question the agent asked last if any, and the user's NEW message. "
    "Work out what the user wants now, keeping every earlier goal:\n"
    "- The message changes, narrows or adds detail to a goal (\"use postgres "
    "instead\", \"also handle errors\", \"only the api part\") → that goal, "
    "same id, its text rewritten to include the change. The rest of the goal "
    "stays.\n"
    "- The message adds separate work → a new goal (id null). Earlier open "
    "goals stay open.\n"
    "- The message gives up PART of a goal (one of its pieces) → the same "
    "goal, open, its text rewritten without that piece.\n"
    "- The message says to stop, cancel or forget a WHOLE goal, or clearly "
    "replaces it with something else → that goal's status \"dropped\". "
    "Never drop a goal the message does not clearly give up.\n"
    "- A question, a status ask, \"ok\" or \"continue\" → goals unchanged.\n"
    "- An answer to THE AGENT ASKED THE USER: apply THE USER'S MESSAGE THAT "
    "QUESTION WAS ABOUT to the goal the answer picks (\"drop it\" + \"the "
    "csv one\" → the csv goal dropped).\n"
    "- A goal marked done that the message continues, redoes or fixes (\"it "
    "still fails\", \"now try X for it\", \"not good enough\") → that goal, "
    "status \"open\" again, its text rewritten with the change. A turn "
    "ending does not mean the user's aim was reached.\n"
    "- A goal marked done is never dropped: undoing or removing done work "
    "(\"remove it\", \"revert that\") is a new goal.\n"
    "\"now\" lists the ids of the goals the agent works on for this message "
    "(\"new\" for a new goal, in the order of new goals added): only goals "
    "this message is about. An unfinished goal the message does not mention "
    "stays open and stays out of \"now\" (\"continue\" is about the "
    "unfinished ones; a question or a status ask gives []). A message sent WHILE the agent works keeps every goal "
    "in THE AGENT IS WORKING ON in \"now\" unless it drops that goal. \"current\" "
    "states, in the user's own names (repositories, files, hosts, numbers "
    "exactly as written), everything the agent must achieve now: the goals in "
    "\"now\" with every change applied. Self-contained: readable without the "
    "chat.\n"
    "Only when you cannot tell how the message relates to the goals — it "
    "could mean replacing a goal or adding to it; \"that one\", \"it\", "
    "\"the other\" could point at more than one goal or piece of a goal; or "
    "guessing wrong would throw work away — set \"question\" "
    "to ONE short question for the user; otherwise \"question\" is \"\". "
    "Do not ask about details the agent can find out itself.\n"
    "Reply with JSON only, no prose:\n"
    "{\"goals\": [{\"id\": \"g1\", \"status\": \"open\", \"text\": \"…\"}, "
    "{\"id\": null, \"status\": \"open\", \"text\": \"…\"}], "
    "\"now\": [\"g1\", \"new\"], \"current\": \"…\", \"question\": \"\"}")


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_GOALS", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _path(session_id) -> str:
    from aiforge_core.runtime import repo_ident
    return os.path.join(repo_ident.chat_scratch_root(), f"session-{session_id}",
                        ".aiforge", "goals.json")


def _temporary(session_id) -> bool:
    try:
        from aiforge_core.runtime import chat_private
        return bool(chat_private.is_temporary(session_id))
    except Exception:  # noqa: BLE001
        return False


def _empty() -> dict:
    return {"goals": [], "now": [], "current": "", "asked": "", "asked_about": "",
            "seq": 0}


def _load(session_id) -> dict:
    if _temporary(session_id):
        return json.loads(json.dumps(_MEM.get(str(session_id)) or _empty()))
    try:
        with open(_path(session_id), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    out = _empty()
    out["goals"] = [g for g in data.get("goals") or []
                    if isinstance(g, dict) and g.get("id") and g.get("text")]
    ids = {g["id"] for g in out["goals"]}
    out["now"] = [i for i in data.get("now") or [] if i in ids]
    out["current"] = str(data.get("current") or "")
    out["asked"] = str(data.get("asked") or "")
    out["asked_about"] = str(data.get("asked_about") or "")
    try:
        out["seq"] = int(data.get("seq") or 0)
    except (TypeError, ValueError):
        out["seq"] = 0
    return out


def _save(session_id, data: dict) -> None:
    if _temporary(session_id):
        _MEM[str(session_id)] = data
        return
    from aiforge_core.config import _atomic
    path = _path(session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic.write_text(path, json.dumps(data, indent=1))


def goals(session_id) -> dict:
    """The chat's goal record (empty when off)."""
    if not enabled() or session_id is None:
        return _empty()
    try:
        with _LOCK:
            return _load(session_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("goals read failed (session=%s): %s", session_id, exc)
        return _empty()


def _flat(text, n: int) -> str:
    return " ".join(str(text or "").split())[:n]


def _users_words(text: str) -> str:
    """The user's own words: blocks the harness appends are cut."""
    try:
        from aiforge_core.runtime.chat_agent._turn._convo import strip_turn_note
        text = strip_turn_note(text or "")
    except Exception:  # noqa: BLE001
        pass
    return str(text or "").split("\n\n---\n[")[0].strip()


def _open(data: dict) -> list:
    return [g for g in data["goals"] if g.get("status") == OPEN]


def _new_id(data: dict) -> str:
    data["seq"] = int(data.get("seq") or 0) + 1
    return f"g{data['seq']}"


def _says_nothing(text: str, said: str = "") -> bool:
    """"continue", "go on", "yes do it": no change to any goal, no call. Not
    after the agent asked something ("Want tests too?" — "yes" is a new
    goal), which the model reads with the answer."""
    if str(said or "").rstrip().endswith("?"):
        return False
    try:
        from aiforge_core.runtime.chat_resume import _CONTINUE_RE
        if _CONTINUE_RE.match(text):
            return True
    except Exception:  # noqa: BLE001
        pass
    return text.strip().lower().rstrip(".!") in (
        "ok", "okay", "go", "go on", "go ahead", "proceed", "continue",
        "carry on", "keep going", "yes", "y", "yes go", "do it")


# ── the model call ──────────────────────────────────────────────────────────

def _ask(role: str, user: str, session_id) -> "str | None":
    from aiforge_core.runtime.chat_agent._turn import _plan_llm
    return _plan_llm._ask(role, _SYSTEM, user, session_id, max_tokens=2000)


def _reply_json(text: "str | None") -> "dict | None":
    """The last JSON object in ``text`` with a ``goals`` list (a reasoning
    model thinks first and answers at the end), else None."""
    if not text:
        return None
    dec = json.JSONDecoder()
    found = None
    i = text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict) and isinstance(obj.get("goals"), list):
            found = obj
        i = text.find("{", end)
    return found


def _shown(data: dict) -> list:
    done = [g for g in data["goals"] if g.get("status") == DONE][-_SHOWN_DONE:]
    return [g for g in data["goals"]
            if g.get("status") == OPEN or g in done]


def _prompt(data: dict, message: str, mid_run: bool, said: str,
            working_on: str = "") -> str:
    rows = [{"id": g["id"], "status": g["status"], "text": g["text"],
             **({"later changes": g["updates"]} if g.get("updates") else {})}
            for g in _shown(data)]
    parts = [f"GOALS SO FAR:\n{json.dumps(rows, ensure_ascii=False, indent=1)}"]
    if data.get("now"):
        parts.append(("THE AGENT IS WORKING ON: " if mid_run else
                      "THE LAST TURN STOPPED BEFORE FINISHING: ")
                     + ", ".join(data["now"]))
    if working_on:
        parts.append(f"THE STEP THE AGENT IS ON RIGHT NOW: {_flat(working_on, 400)}")
    if said:
        parts.append(f"THE AGENT'S LAST ANSWER (context only):\n{_flat(said, 1200)}")
    if data.get("asked"):
        parts.append(f"THE AGENT ASKED THE USER: {data['asked']}")
        if data.get("asked_about"):
            parts.append("THE USER'S MESSAGE THAT QUESTION WAS ABOUT: "
                         + _flat(data["asked_about"], 1500))
    when = ("sent WHILE the agent was working" if mid_run
            else "sent as a new message")
    parts.append(f"THE USER'S NEW MESSAGE ({when}):\n{message[:6000]}")
    return "\n\n".join(parts)


def _apply(data: dict, reply: dict, message: str, turn,
           mid_run: bool = False) -> "tuple[dict, list]":
    """``data`` after the model's reply, and one line per change. An earlier
    goal the reply leaves out stays as it was; a goal is dropped only when
    the reply says so; mid-run, the goals being worked on stay worked on."""
    prev_now = list(data.get("now") or [])
    by_id = {g["id"]: g for g in data["goals"]}
    changes: list = []
    new_ids: list = []
    seen: set = set()
    wanted = {str(x or "").strip() for x in reply.get("now") or []}
    for row in reply.get("goals") or []:
        if not isinstance(row, dict):
            continue
        gid = str(row.get("id") or "").strip()
        text = _flat(row.get("text"), _GOAL_CHARS)
        status = str(row.get("status") or OPEN).strip().lower()
        if status not in (OPEN, DONE, DROPPED):
            status = OPEN
        if gid in by_id:
            if gid in seen:
                continue
            seen.add(gid)
            g = by_id[gid]
            if status == DROPPED and g["status"] == OPEN:
                g["status"] = DROPPED
                g["closed_turn"] = turn
                changes.append(f"dropped {gid}: {g['text'][:160]}")
                continue
            if status == OPEN and g["status"] == DONE and gid in wanted:
                g["reopened_from"] = g["status"]
                g["status"] = OPEN           # the user wants more of it
                g["reopened_turn"] = turn
                changes.append(f"reopened {gid}")
            if text and text != g["text"] and g["status"] == OPEN:
                prev = g["text"]
                g["text"] = text
                ups = [u for u in g.get("updates") or [] if isinstance(u, dict)]
                ups.append({"text": _flat(message, _UPDATE_CHARS), "turn": turn,
                            "prev": prev})
                g["updates"] = ups[-_MAX_UPDATES:]
                changes.append(f"changed {gid}: {text[:160]}")
            continue
        if not text or status != OPEN:
            continue
        same = next((g for g in data["goals"] if g["text"] == text), None)
        if same is not None:
            # The user asks again for a goal that was dropped or done: it is
            # that goal, open again — not a duplicate to skip.
            if same["status"] != OPEN:
                same["reopened_from"] = same["status"]
                same["status"] = OPEN
                same["reopened_turn"] = turn
                changes.append(f"reopened {same['id']}")
            if same["id"] not in new_ids:
                new_ids.append(same["id"])
            continue
        gid = _new_id(data)
        g = {"id": gid, "text": text, "status": OPEN, "turn": turn,
             "at": int(time.time())}
        data["goals"].append(g)
        by_id[gid] = g
        new_ids.append(gid)
        changes.append(f"added {gid}: {text[:160]}")
    now: list = []
    fresh = iter(new_ids)
    for x in reply.get("now") or []:
        x = str(x or "").strip()
        gid = next(fresh, None) if x.lower() in ("new", "null", "none", "") else x
        if gid in by_id and by_id[gid]["status"] == OPEN and gid not in now:
            now.append(gid)
    now += [g for g in new_ids if g not in now]
    if not now and (reply.get("now") or str(reply.get("current") or "").strip()):
        # The reply named work it did not list as a goal: the message is that
        # work (never lose it to an empty "now").
        text = _flat(reply.get("current") or message, _GOAL_CHARS)
        gid = _new_id(data)
        g = {"id": gid, "text": text, "status": OPEN, "turn": turn,
             "at": int(time.time())}
        data["goals"].append(g)
        by_id[gid] = g
        now = [gid]
        changes.append(f"added {gid}: {text[:160]}")
    kept = []
    if mid_run:
        # A steer adds to the work in hand; it never takes a goal off it
        # without dropping that goal.
        kept = [g for g in prev_now if g in by_id and by_id[g]["status"] == OPEN
                and g not in now]
        now = kept + now
    # Empty: the message works on no goal (a question, a thank-you). The turn
    # then closes nothing.
    data["now"] = now
    cur = _flat(reply.get("current"), _CURRENT_CHARS)
    if kept or not cur:
        cur = _flat(" / ".join(by_id[g]["text"] for g in now), _CURRENT_CHARS)
    data["current"] = cur if now else ""     # no goal in hand: no goal statement
    return data, changes


#: The user's words for giving work up as a whole. A reply that leaves no
#: goal open on a message without one of these is confirmed with the user
#: first: "skip that one" under a two-part goal is not "stop everything".
_GIVE_UP_RE = re.compile(
    r"\b(?:stop|cancel|abort|scrap|halt|never\s*mind|nevermind|forget|"
    r"start over|from scratch|all of it|the whole lot|"
    r"whole (?:thing|task)|don'?t (?:do|bother|need)|no need)\b", re.I)


def _drops_everything(before_open: list, data: dict, message: str) -> bool:
    return (bool(before_open) and not _open(data)
            and not _GIVE_UP_RE.search(message))


#: Words that point at a goal without naming it.
_VAGUE_RE = re.compile(r"\b(?:it|that|this|those|them|the other|that one|"
                       r"this one|the last one|the first one)\b", re.I)


#: Orders a vague "it" can carry.
_ACT_RE = re.compile(r"\b(?:drop|remove|skip|cancel|stop|undo|revert|redo|"
                     r"delete|forget|scrap|finish|retry)\b", re.I)


def _vague_pick(before: dict, changes: list, message: str,
                mid_run: bool = False) -> str:
    """A short order that names no goal ("drop it", "redo that one") while
    several goals could be meant: the question to ask instead of guessing
    ("" when it is clear enough)."""
    if len(message.split()) > 8 or not _VAGUE_RE.search(message) \
            or not _ACT_RE.search(message):
        return ""
    if mid_run:
        # While the agent works, "it" is the work in hand.
        cands = [g for g in _open(before) if g["id"] in (before.get("now") or [])]
    else:
        # What "it" can mean: the open goals and the ones the last turn finished.
        done = [g for g in before["goals"] if g.get("status") == DONE]
        last = max((g.get("closed_turn") or 0 for g in done), default=0)
        cands = _open(before) + [g for g in done if (g.get("closed_turn") or 0) == last]
    if len(cands) < 2:
        return ""
    names = "; ".join(f"({i + 1}) {_flat(g['text'], 120)}"
                      for i, g in enumerate(cands[-4:]))
    # The order is restated: the answer ("the csv one") must make sense on
    # its own, to the agent as well as to the goal model.
    return f'You said "{_flat(message, 80)}" — which one do you mean: {names}?'



def _confirm_drop(goals_before: list) -> str:
    what = "; ".join(_flat(g["text"], 200) for g in goals_before[:3])
    return (f"To be sure: should I stop all of this — {what}? "
            "Or drop only one part of it (which one)?")


def _fallback(data: dict, message: str, mid_run: bool, turn) -> "tuple[dict, list]":
    """No answer from the model: nothing is dropped. A steer is a change to
    the goals being worked on; a new message is a goal of its own."""
    text = _flat(message, _GOAL_CHARS)
    if mid_run and data.get("now"):
        # A steer: a change to the work in hand, never a replacement for it.
        for gid in data["now"]:
            g = next((x for x in data["goals"] if x["id"] == gid), None)
            if g is None:
                continue
            ups = [u for u in g.get("updates") or [] if isinstance(u, dict)]
            ups.append({"text": _flat(message, _UPDATE_CHARS), "turn": turn})
            g["updates"] = ups[-_MAX_UPDATES:]
        data["current"] = _flat(
            (data.get("current") or "") + " — and, sent later: " + text,
            _CURRENT_CHARS)
        return data, ["kept every goal; the message is a change to the work"]
    gid = _new_id(data)
    data["goals"].append({"id": gid, "text": text, "status": OPEN, "turn": turn,
                          "at": int(time.time())})
    data["now"] = [gid]
    data["current"] = text
    return data, [f"added {gid}: {text[:160]}"]


def _trim(data: dict) -> None:
    while len(data["goals"]) > MAX_GOALS:
        closed = [g for g in data["goals"] if g["status"] != OPEN]
        if not closed:
            break                    # an open goal is never trimmed away
        data["goals"].remove(closed[0])
    ids = {g["id"] for g in data["goals"]}
    data["now"] = [i for i in data["now"] if i in ids]


def on_message(session_id, message: str, *, role: str = "chat",
               mid_run: bool = False, said: str = "", turn=None,
               working_on: str = "", llm: bool = True) -> dict:
    """Bring the chat's goals in line with ``message`` (a new turn's request,
    or a steer when ``mid_run``). Returns ``{"current", "question", "changes",
    "called"}``: the goal statement for the work now, a question for the user
    when the message could not be placed ("" otherwise), one line per change.
    Never raises."""
    out = {"current": "", "question": "", "changes": [], "called": False}
    text = _users_words(message)
    if not enabled() or session_id is None or not text:
        return out
    if not mid_run:
        _PASSED.add(str(session_id))
        _ENDED_PASSED.discard(str(session_id))
    elif llm and str(session_id) not in _PASSED:
        # A run that did not set the chat's goals (a plan being executed, a
        # wake turn): its steer would become the whole goal. Left alone.
        return out
    if turn is None:
        turn = _user_turns(session_id)
    try:
        with _session_lock(session_id):
            _update(session_id, text, out, role=role, mid_run=mid_run,
                    said=said, turn=turn, working_on=working_on, llm=llm)
    except Exception as exc:  # noqa: BLE001 — never break a turn over goals
        log.debug("goals update failed (session=%s): %s", session_id, exc)
    return out


def _update(session_id, text, out, *, role, mid_run, said, turn, working_on,
            llm=True):
    data = _load(session_id)
    was_now = list(data.get("now") or [])
    if not llm:
        # Kept without a model call (a steer the stopped run never read).
        data, changes = _fallback(data, text, mid_run, turn)
        _trim(data)
        _save(session_id, data)
        out["current"], out["changes"] = data.get("current") or "", changes
        return
    if not _shown(data):
        if _says_nothing(text, said) and data["goals"]:
            return
        # The chat's first goal: the message is the goal. No model call.
        data["asked"] = data["asked_about"] = ""
        data, changes = _fallback(data, text, False, turn)
    elif _says_nothing(text, said) and not data.get("asked"):
        # "continue": the unfinished goals are the work.
        if not data.get("now") and _open(data):
            data["now"] = [g["id"] for g in _open(data)]
            data["current"] = _flat(" / ".join(g["text"] for g in _open(data)),
                                    _CURRENT_CHARS)
            _save(session_id, data)
            out["resumed"] = True     # the note says which goal "continue" means
        out["current"] = data.get("current") or ""
        return
    else:
        reply = _reply_json(_ask(
            role, _prompt(data, text, mid_run, said, working_on), session_id))
        out["called"] = True
        asked_before = bool(data.get("asked"))
        before = json.loads(json.dumps(data))
        q = ""
        if reply is None:
            data, changes = _fallback(data, text, mid_run, turn)
        else:
            data, changes = _apply(data, reply, text, turn, mid_run)
            out["only_drops"] = (not reply.get("now")
                                 and not str(reply.get("current") or "").strip()
                                 and "?" not in text)
            q = _flat(reply.get("question"), 400)
            if not q and not asked_before:
                q = (_vague_pick(before, changes, text, mid_run)
                     or (_confirm_drop(_open(before))
                         if _drops_everything(_open(before), data, text)
                         else ""))
        # One question per message: an answer that is still unclear gets the
        # best reading, not a second question.
        if q and not asked_before:
            # Nothing changes until the user answers.
            data, changes = before, []
            out["question"] = q
            data["asked"], data["asked_about"] = q, text
        else:
            data["asked"] = data["asked_about"] = ""
    if not mid_run and changes:
        # The stopped turn's work itself was dropped: its handoff ("carry on
        # from NEXT") must not lead this turn back into it.
        in_hand = [g for g in data["goals"] if g["id"] in was_now]
        out["dropped_stopped"] = bool(in_hand) and \
            any(g["status"] == DROPPED for g in in_hand) and \
            not any(g["status"] == OPEN for g in in_hand)
    _trim(data)
    _save(session_id, data)
    out["current"] = data.get("current") or ""
    out["changes"] = changes


def finish_turn(session_id, *, completed: bool) -> None:
    """The turn ended. ``completed`` (an answer, not stopped, not waiting on
    the user, no task left open): the goals it worked on are done. Otherwise
    they stay open for the next message. A turn that did not run the goal
    pass (a wake turn, a plan being executed) closes nothing."""
    if session_id is None:
        return
    passed = str(session_id) in _PASSED
    _PASSED.discard(str(session_id))
    if passed:
        _ENDED_PASSED.add(str(session_id))
    if not enabled() or not completed or not passed:
        return
    try:
        with _session_lock(session_id):
            data = _load(session_id)
            if not data["now"] or data.get("asked"):
                return
            turn = _user_turns(session_id)
            for g in data["goals"]:
                if g["id"] in data["now"] and g["status"] == OPEN:
                    g["status"] = DONE
                    g["closed_turn"] = turn
            data["now"] = []
            data["current"] = ""
            _save(session_id, data)
    except Exception as exc:  # noqa: BLE001
        log.debug("goals close failed (session=%s): %s", session_id, exc)


def _user_turns(session_id) -> "int | None":
    try:
        from aiforge_core.runtime import chat_store
        return sum(1 for r in chat_store.get_messages(session_id)
                   if r.get("role") == "user")
    except Exception:  # noqa: BLE001
        return None


def _just_dropped(data: dict) -> list:
    """Goals the user dropped with the newest change: kept in view for one
    turn, so the agent stops the work instead of carrying on with it."""
    drops = [g for g in data["goals"] if g.get("status") == DROPPED]
    last = max((g.get("closed_turn") or 0 for g in drops), default=0)
    return [g for g in drops if (g.get("closed_turn") or 0) == last] if last else []


def render(data: dict) -> str:
    """The block for ``data`` ("" when there are no goals to show)."""
    opened = _open(data)
    done = [g for g in data["goals"] if g.get("status") == DONE][-_SHOWN_DONE:]
    if not opened and not done and not _just_dropped(data):
        return ""
    now = set(data.get("now") or [])
    lines = [f"{MARK} — every goal the user set, kept across messages. Work "
             "toward GOAL NOW; never drop or contradict another open goal "
             "unless the user says so. A later change to a goal overrides its "
             "earlier wording."]
    if data.get("current") and now:
        lines.append(f"GOAL NOW: {data['current']}")
    for g in opened:
        tag = "working on now" if g["id"] in now else "open, not this turn"
        lines.append(f"- {g['id']} ({tag}): {g['text']}")
        for u in (g.get("updates") or [])[-3:]:
            lines.append(f"    later the user said: \"{_flat(u.get('text'), 300)}\"")
    for g in done:
        lines.append(f"- {g['id']} (done earlier): {_flat(g['text'], 300)}")
    for g in _just_dropped(data):
        lines.append(f"- {g['id']} (DROPPED by the user — do no more work on "
                     f"it): {_flat(g['text'], 300)}")
    if not now:
        lines.append("This message sets no goal work: answer it or confirm "
                     "the change, and do not start an open goal on your own.")
    if [g for g in opened if g["id"] not in now]:
        lines.append("Open goals not worked on this turn stay open: do not "
                     "start them on your own, and do not drop or undo them.")
    if data.get("asked"):
        lines.append(f"Waiting on the user's answer to: {data['asked']}")
    text = "\n".join(lines)
    return text[:_BLOCK_CHARS]


def block(session_id) -> str:
    """The GOALS block for a chat ("" when off / no goals)."""
    if not enabled() or session_id is None:
        return ""
    try:
        return render(goals(session_id))
    except Exception:  # noqa: BLE001
        return ""


#: Opening of the note a turn's message carries (stripped from the goal text).
TURN_NOTE_MARK = "\n\n---\n[GOALS — the harness"


def turn_note(out: dict) -> str:
    """The note put under the user's message when the goal pass changed the
    goals: what was dropped, and the goal now (or that there is none). Next
    to the message, where a handoff's "carry on" would otherwise win."""
    changes = out.get("changes") or []
    if not ((out.get("called") and changes) or out.get("resumed")):
        return ""
    dropped = [c.split(": ", 1)[-1] for c in changes if c.startswith("dropped")]
    lines = [TURN_NOTE_MARK.strip("\n-") + ", not the user]"]
    for d in dropped:
        lines.append(f"The user DROPPED this goal — do no more work on it: {d}")
    lines.append(f"GOAL NOW: {out['current']}" if out.get("current") else (
        "No work for this message: the change above is all it asks. Confirm "
        "it to the user in one line, and do not start, continue or undo any "
        "work."))
    return "\n\n---\n" + "\n".join(lines)


def drop_reply(session_id, out: dict) -> str:
    """The harness's own answer to a message that only dropped goals (no
    work left for it): what was dropped and what is still open. "" when the
    message set work or dropped nothing. An agent run here only pulled the
    dropped work back in from the history."""
    changes = out.get("changes") or []
    dropped = [c.split(": ", 1)[-1] for c in changes if c.startswith("dropped")]
    if not out.get("called") or out.get("current") or not dropped \
            or any(not c.startswith("dropped") for c in changes) \
            or not out.get("only_drops"):
        return ""
    lines = ["OK — dropped: " + "; ".join(_flat(d, 200).rstrip(".") for d in dropped) + "."]
    still = _open(goals(session_id))
    if still:
        lines.append("Still open: " + "; ".join(_flat(g["text"], 160) for g in still)
                     + ' — say "continue" to work on it.')
    lines.append("Work already done for it stays as it is; ask if it should "
                 "be removed.")
    return "\n".join(lines)


def ask_directive(question: str) -> str:
    """What the running agent is told when a steer could not be placed."""
    return ("[GOALS — the harness, not the user] It is not clear how this "
            "message changes the goals. Do not guess and do not drop any "
            "goal: stop and ask the user, exactly one line:\n"
            f"ASK: {question}")


def now_directive(current: str, changes: list) -> str:
    """The goal statement the running agent works to after a steer."""
    if not current:
        return ""
    head = "[GOALS — the harness, not the user] "
    what = ("; ".join(changes[:4]) + ". ") if changes else ""
    return f"{head}{what}GOAL NOW (all of it): {current}"


def keep_unread(session_id, texts) -> None:
    """Steers a stopped run never read: their change to the goals is kept
    without a model call — only for a run that set the chat's goals, and
    never a give-up ("never mind") made into a goal."""
    if session_id is None or str(session_id) not in _ENDED_PASSED:
        return
    _ENDED_PASSED.discard(str(session_id))
    for text in texts:
        if text and not _GIVE_UP_RE.search(text):
            on_message(session_id, text, mid_run=True, llm=False)


def drop_after_turn(session_id, turn: int) -> None:
    """The chat was cut back to ``turn`` user messages (edit-and-resend):
    goals and changes from the turns that are gone are taken back."""
    if session_id is None:
        return
    try:
        with _session_lock(session_id):
            data = _load(session_id)
            gone = lambda t: isinstance(t, int) and t > turn  # noqa: E731
            data["goals"] = [g for g in data["goals"] if not gone(g.get("turn"))]
            for g in data["goals"]:
                ups = [u for u in g.get("updates") or [] if isinstance(u, dict)]
                while ups and gone(ups[-1].get("turn")):
                    u = ups.pop()
                    if u.get("prev"):
                        g["text"] = u["prev"]
                g["updates"] = ups
                if gone(g.get("closed_turn")):
                    g["status"] = OPEN
                    g.pop("closed_turn", None)
                if gone(g.get("reopened_turn")):
                    g["status"] = g.pop("reopened_from", None) or DONE
                    g.pop("reopened_turn", None)
            # The next message works out what the work is again.
            data["now"], data["current"] = [], ""
            data["asked"] = data["asked_about"] = ""
            _save(session_id, data)
    except Exception as exc:  # noqa: BLE001
        log.debug("goals write failed (session=%s): %s", session_id, exc)


def forget(session_id) -> None:
    """The chat was deleted: drop its goals."""
    if session_id is None:
        return
    with _session_lock(session_id):       # not under an update in flight
        _MEM.pop(str(session_id), None)
        _PASSED.discard(str(session_id))
        _ENDED_PASSED.discard(str(session_id))
        try:
            os.remove(_path(session_id))
        except OSError:
            pass
    with _LOCK:
        _SESSION_LOCKS.pop(str(session_id), None)


__all__ = ["MARK", "TURN_NOTE_MARK", "ask_directive", "drop_reply", "keep_unread", "turn_note", "block", "drop_after_turn", "enabled",
           "finish_turn", "forget", "goals", "now_directive", "on_message",
           "render"]
