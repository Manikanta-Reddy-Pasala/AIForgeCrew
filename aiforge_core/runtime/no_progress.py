"""The second loop class: busy, varied, and going nowhere.

:mod:`aiforge_core.runtime.same_failure` catches a failure that survives fix
after fix. A run can also loop without ever failing: thirty different
``python3 << 'EOF'`` scripts that each try another guess and exit 0 printing
"no match" — no file changes, no new file read, no failure, and args that
differ every time, so neither the exact-repeat guard nor the same-failure
rule ever trips.

This module counts steps between two pieces of PROGRESS. The caller decides
what progress is (a new workspace state, a file or page read for the first
time, a task marked done, fewer failing tests, a running command that is
still producing output); this module only sees ``observe(..., progress)``.
Two rules, both reset by any progress:

* similar-action streak — ``_STREAK`` of the last ``_WINDOW`` steps share a
  command template (the leading non-comment lines of a shell command) or a
  normalised output, with no progress in the window;
* no progress at all for ``AIFORGE_NO_PROGRESS_STEPS`` (default 25) steps
  in a row. Not a step cap: the count starts over on any progress.

A trip spends the same one-shot warning budget as the same-failure rule
(``track["trips"]``): the first trip of either kind nudges, the next one
stops. The tracker is a plain dict, so a pipeline can keep it in its state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter

from aiforge_core.runtime.failure_signature import _message

#: Steps looked at, and how many of them must look alike.
_WINDOW = 8
_STREAK = 6
_TEMPLATE_LINES = 2
_TEMPLATE_CHARS = 120
_OUTPUT_CHARS = 400
_SHELL_TOOLS = frozenset({"run_command", "bash", "shell", "run", "run_shell"})

NUDGE = "nudge"
STOP = "stop"

_COMMENT = re.compile(r"^\s*(#|//|--\s)")
_TRAILING_COMMENT = re.compile(r"\s+#[^'\"]*$")
_SPACE = re.compile(r"\s+")


def no_progress_steps() -> int:
    """Steps in a row with no progress of any kind before the run is told
    to step back (``AIFORGE_NO_PROGRESS_STEPS``, default 25, at least 8)."""
    try:
        val = int(os.environ.get("AIFORGE_NO_PROGRESS_STEPS", "25"))
    except ValueError:
        return 25
    return max(_WINDOW, val)


def command_template(name: str, args) -> str:
    """What kind of step this is. A shell command is its first non-comment
    lines, whitespace collapsed and cut short: thirty brute-force scripts
    that open with the same ``python3 << 'EOF'`` / ``import hashlib`` read as
    one template, whatever each tries further down. Any other tool is itself
    with its exact arguments: exploring means different arguments."""
    args = args if isinstance(args, dict) else {}
    if name in _SHELL_TOOLS:
        cmd = str(args.get("cmd") or args.get("command") or "")
        lines = [_SPACE.sub(" ", _TRAILING_COMMENT.sub("", ln)).strip()
                 for ln in cmd.splitlines() if ln.strip() and not _COMMENT.match(ln)]
        head = " / ".join(lines[:_TEMPLATE_LINES])[:_TEMPLATE_CHARS]
        return f"{name}:{head}" if head else ""
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = str(args)
    return f"{name}:" + hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:16]  # noqa: S324


_SHELL_READERS = frozenset({"cat", "head", "tail", "sed", "less", "more", "bat",
                            "nl", "wc", "awk", "grep", "rg", "ls", "find", "tree"})
_PATHISH = re.compile(r"^[\w./~-]*[/.][\w./~-]*$")


def shell_read_paths(args) -> list[str]:
    """The paths a one-line shell read names (``cat a.py``, ``sed -n
    1,80p b.py``): reading through the shell is still reading. A script or
    any other command names none."""
    import shlex
    cmd = str((args or {}).get("cmd") or (args or {}).get("command") or "")
    if "\n" in cmd or "<<" in cmd:
        return []
    try:
        words = shlex.split(cmd)
    except ValueError:
        return []
    if not words or words[0] not in _SHELL_READERS:
        return []
    return [w for w in words[1:] if not w.startswith("-") and _PATHISH.match(w)]


def output_class(name: str, text: str) -> str:
    """A shell command's output with numbers, paths and timings masked:
    "tried 1000, no match" and "tried 2000, no match" are one class. Empty
    output has no class (plenty of healthy commands print nothing)."""
    if name not in _SHELL_TOOLS:
        return ""
    norm = _message(str(text or "")[-_OUTPUT_CHARS:])
    return f"out:{norm}" if norm else ""


def observe(track: dict, template: str, out_class: str, progress: bool) -> str:
    """Record one step. Returns ``""``, ``"nudge"`` or ``"stop"``.

    ``progress`` is the caller's verdict for this step; it empties the
    window and the no-progress count."""
    st = track.setdefault("np", {"window": [], "idle": 0})
    if progress:
        st["window"], st["idle"] = [], 0
        return ""
    st["window"] = (st["window"] + [[template, out_class]])[-_WINDOW:]
    st["idle"] += 1
    reason = _reason(st)
    if not reason:
        return ""
    st["window"], st["idle"] = [], 0      # a fresh count to act on the nudge
    track["trips"] = int(track.get("trips", 0)) + 1
    track["last"] = reason
    return NUDGE if track["trips"] == 1 else STOP


def would_trip(track: dict, template: str, out_class: str) -> bool:
    """Whether one more idle step like this one would trip a rule — so a
    caller can check a costly progress signal only when it matters."""
    st = track.get("np") or {"window": [], "idle": 0}
    trial = {"window": (st["window"] + [[template, out_class]])[-_WINDOW:],
             "idle": st["idle"] + 1}
    return bool(_reason(trial))


def _reason(st: dict) -> str:
    if st["idle"] >= no_progress_steps():
        return f"{st['idle']} steps without progress"
    window = st["window"]
    if len(window) < _STREAK:
        return ""
    for i, what in ((0, "command"), (1, "output")):
        common = Counter(w[i] for w in window if w[i]).most_common(1)
        if common and common[0][1] >= _STREAK:
            return f"{common[0][1]} similar steps ({what}: {common[0][0][:80]})"
    return ""


def nudge_text(reason: str = "") -> str:
    return ("[loop guard — not the user] You have taken many steps that make "
            "no progress" + (f" ({reason})" if reason else "") + ": nothing "
            "changed in the workspace, nothing new was read, no test moved. "
            "Stop and say, in two or three sentences, what you have tried and "
            "why it is not converging. Then change approach — or, if the goal "
            "looks impossible as stated (e.g. guessing a value only a hash "
            "knows), say so plainly and ask the user.")


def stop_text(reason: str = "") -> str:
    return ("I've been trying variations without getting any closer"
            + (f" ({reason})" if reason else "")
            + ". I've paused rather than keep going — could you check whether "
              "the goal is reachable, or tell me how you'd like me to proceed?")


__all__ = ["NUDGE", "STOP", "no_progress_steps", "command_template",
           "output_class", "shell_read_paths", "observe", "would_trip", "nudge_text", "stop_text"]
