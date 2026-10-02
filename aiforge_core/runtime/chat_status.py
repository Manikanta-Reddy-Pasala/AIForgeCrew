"""Answer "what is the status?" from a running chat turn, without a model.

While a run is going, a message like "status?" or "how far along are you?" used
to take one of two bad paths: queued as a steer, which the running agent folded
into its context at its next step (often minutes away, and the reply could be
discarded as a stale draft), or started as a side task, a separate agent that
knows nothing about the run. Neither could say what the run was doing.

The run already knows: ``chat_runs`` records the tool in flight, the last few
finished calls, the phase, how long it has been quiet and any model-wait line.
This turns that into a short answer, instantly, and leaves the run alone.
"""
from __future__ import annotations

import re
import time

# A status question is short. Anything longer is a task that happens to
# contain the word.
_MAX_WORDS = 14

_ONLY = re.compile(r"^(?:status|progress|update|eta|sitrep)(?:\s+(?:please|pls|now|report))?$")
_PATTERNS = [re.compile(p) for p in (
    r"\b(?:what(?:'s| is| are| s)?|whats)\b.{0,24}\b(?:status|progress|happening|going on|state|doing|up to)\b",
    r"\bhow\b.{0,16}\b(?:far|long|much (?:is )?left|is it going|are you doing|is this going|goes it)\b",
    r"\b(?:are|is)\s+(?:you|it|this|that)\s+(?:still\s+)?(?:working|running|stuck|done|finished|alive|hung|frozen|there|going|progressing)\b",
    r"\b(?:any|an?|the|your|latest)\s+(?:update|progress|status)\b",
    r"\b(?:give|send|show|tell)\s+me\s+(?:an?\s+|the\s+)?(?:update|status|progress)\b",
    r"\bwhere\s+(?:are|is)\s+(?:you|we|it|this)\b",
    r"\bwhat\s+(?:step|stage|part)\b",
    r"\b(?:is\s+)?anything\s+(?:happening|going on|moving)\b",
    r"\bstill\s+(?:there|alive|working|running|on it)\b",
    r"\bhow(?:'s| is)\s+it\s+going\b",
)]


def is_status_request(text: str) -> bool:
    """True when ``text`` only asks how the running work is going."""
    raw = (text or "").strip()
    if not raw:
        return False
    t = re.sub(r"[^\w\s'?]", " ", raw.lower()).replace("?", " ")
    t = " ".join(t.split())
    if not t or len(t.split()) > _MAX_WORDS:
        return False
    if _ONLY.match(t):
        return True
    try:
        from aiforge_core.api.routes._chat._overlap import has_edit_intent
        if has_edit_intent(raw):
            return False                 # "update the README file" is a task
    except Exception:  # noqa: BLE001
        pass
    return any(p.search(t) for p in _PATTERNS)


_CD_PREFIX = re.compile(r"^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)+")
_READ_TOOLS = ("read_file", "file_read", "grep", "find", "list_dir", "list_files",
               "glob", "repo_map", "search", "web_read", "web_search")
_CHANGE_TOOLS = ("write_file", "file_write", "edit_file", "apply_patch", "patch",
                "str_replace", "delete_file", "move_file")


def friendly_cmd(cmd: str, limit: int = 60) -> str:
    """A command as a person would say it: the leading ``cd <folder> &&`` is
    dropped, a long path to a program is shortened to its name
    (``.aiforge-venv/bin/python`` -> ``python``), and it is cut at a word with
    an ellipsis rather than mid-token."""
    text = _CD_PREFIX.sub("", cmd or "").strip()
    parts = []
    for tok in text.split():
        if "/" in tok and len(tok) > 12 and not tok.startswith(("-", "http")):
            tok = tok.rstrip("/").rsplit("/", 1)[-1] or tok
        parts.append(tok)
    text = " ".join(parts)
    if len(text) > limit:
        text = text[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;&|") + "…"
    return text


def describe_activity(name: str, args: str = "") -> str:
    """What a tool call means, in plain words (no tool names, no raw paths)."""
    n = (name or "").strip()
    if n == "run_command":
        cmd = friendly_cmd(args)
        return f"running `{cmd}`" if cmd else "running a command"
    if n in ("command_wait", "command_output"):
        return "waiting for a command to finish"
    if n in _CHANGE_TOOLS:
        return "writing files"
    if n in _READ_TOOLS:
        return "reading the project"
    return f"working with {n.replace('_', ' ')}" if n else "working"


def _call(name: str, args: str) -> str:
    """A tool call as markdown. Names carry underscores, which markdown reads
    as emphasis ("write_file" became "writefile"), so they go in code spans; a
    path is shown by its file name."""
    arg = (args or "").strip()
    if name == "run_command":
        arg = friendly_cmd(arg, 56)
    elif "/" in arg and " " not in arg:
        arg = arg.rstrip("/").rsplit("/", 1)[-1]
    arg = arg[:56]
    return f"`{name}`" + (f" `{arg}`" if arg else "")


def _dur(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 90:
        return f"{m}m {sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def snapshot(run, *, pending_steers: int = 0, side_tasks: "list | None" = None,
             jobs: "list | None" = None) -> dict:
    """The facts about a live run, as plain data."""
    now = time.time()
    open_calls = sorted(run.open_tools.values(), key=lambda c: c["at"])
    notice, notice_at = run.last_notice
    return {
        "running": not run.done,
        "elapsed_s": round(now - run.started_at, 1),
        "quiet_s": round(now - run.last_event_at, 1),
        "phase": run.phase,
        "steps_done": run.tool_count,
        "in_flight": [{"name": c["name"], "args": c["args"],
                       "for_s": round(now - c["at"], 1)} for c in open_calls[:3]],
        "recent": list(run.recent_tools[-4:]),
        "last_thought": run.last_thought,
        "model_notice": notice if notice and now - notice_at < 300 else "",
        "changes": dict(run.changes),
        "pending_steers": pending_steers,
        "side_tasks": side_tasks or [],
        "commands": jobs or [],
        "answered": bool(getattr(run, "answered", False)),
    }


def render(snap: dict) -> str:
    """The answer, in a few short lines of markdown."""
    if not snap.get("running"):
        return "**Status** — this run has finished."
    if snap.get("answered"):
        return ("**Status** — the answer is out; the run is saving the turn and "
                "finishing up.")
    head = f"**Status** — working for {_dur(snap['elapsed_s'])}"
    if snap["steps_done"]:
        head += f" · {snap['steps_done']} tool call{'s' if snap['steps_done'] != 1 else ''} done"
    lines = [head]
    flight = snap["in_flight"]
    commands = snap.get("commands") or []
    if commands:
        for c in commands[:3]:
            tail = (f", no output for {_dur(c['idle_s'])}" if c.get("idle_s", 0) >= 20 else "")
            lines.append(f"- **Running command:** `{friendly_cmd(c['cmd'], 80)}` — "
                         f"{_dur(c['for_s'])} so far{tail}")
    elif flight:
        for c in flight:
            lines.append(f"- **Now:** {_call(c['name'], c['args'])} — {_dur(c['for_s'])} in")
    elif snap["model_notice"]:
        lines.append(f"- **Now:** {snap['model_notice']}")
    else:
        lines.append(f"- **Now:** {snap['phase'] or 'working'}")
    if commands and flight:
        names = ", ".join(_call(c["name"], c["args"]) for c in flight)
        lines.append(f"- **The agent is:** in {names}, waiting on it")
    if snap["recent"]:
        done = []
        for r in snap["recent"]:
            mark = "✓" if r["ok"] else "✗"
            done.append(f"{_call(r['name'], r['args'])} {mark}")
        lines.append("- **Just done:** " + " → ".join(done))
    if snap["last_thought"]:
        lines.append(f"- **Last thought:** {snap['last_thought'][:160]}")
    ch = snap["changes"]
    if ch and ch.get("files"):
        lines.append(f"- **Changes so far:** {ch['files']} file{'s' if ch['files'] != 1 else ''}"
                     f" (+{ch.get('additions', 0)} −{ch.get('deletions', 0)})")
    quiet = snap["quiet_s"]
    if quiet >= 20:
        lines.append(f"- No new output for {_dur(quiet)} — "
                     + ("that is normal while a command runs." if flight
                        else "waiting on the model."))
    if snap["pending_steers"]:
        n = snap["pending_steers"]
        lines.append(f"- {n} message{'s' if n != 1 else ''} from you waiting to be read.")
    running = [t for t in snap["side_tasks"] if t.get("state") in ("running", "queued")]
    if running:
        lines.append("- **Side tasks:** " + ", ".join(
            f"{(t.get('title') or 'task')[:30]} ({t['state']})" for t in running))
    lines.append("_Nothing was interrupted. Stop ends the run._")
    return "\n".join(lines)


def waiting_on(run) -> str:
    """One plain sentence for a just-sent steer: when will the agent read it.
    No tool names or raw paths: this is shown to the user as the reply to what
    they typed, and it must not read like an error."""
    flight = sorted(run.open_tools.values(), key=lambda c: c["at"])
    if flight:
        c = flight[0]
        return (f"The agent is {describe_activity(c['name'], c['args'])}. It will read your "
                "message as soon as that finishes, usually within seconds. Nothing is stopped.")
    if run.phase == "the model is writing":
        return "The model is in the middle of an answer; the agent reads your message right after."
    if run.phase == "waiting for your approval":
        return "The run is waiting for your approval; it reads your message after that."
    return "The agent will read your message at its next step."


