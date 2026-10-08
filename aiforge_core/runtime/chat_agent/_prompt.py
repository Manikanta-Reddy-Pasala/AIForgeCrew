"""Parsing the model's replies: ACTION / FINAL / ASK steps, and the noise
cleanup around them. The system prompt text lives in ``_prompt_text``."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from ._prompt_text import BATCH_READS_RULE, DECOMPOSE_RULE, LONG_RUN_RULE, _SYSTEM  # noqa: F401  # re-exported
from ._shell import (_ACTION_RE, _ASK_RE, _FINAL_RE, _THOUGHT_RE)

class _BraceScanner:
    """String-aware brace counter — braces inside a JSON string don't nest."""

    __slots__ = ("depth", "in_str", "esc")

    def __init__(self) -> None:
        self.depth = 0
        self.in_str = False
        self.esc = False

    def feed(self, ch: str) -> bool:
        """Consume one char; True when the object just CLOSED."""
        if self.in_str:
            self._feed_in_string(ch)
            return False
        if ch == '"':
            self.in_str = True
        elif ch == "{":
            self.depth += 1
        elif ch == "}":
            self.depth -= 1
            return self.depth == 0
        return False

    def _feed_in_string(self, ch: str) -> None:
        if self.esc:
            self.esc = False
        elif ch == "\\":
            self.esc = True
        elif ch == '"':
            self.in_str = False


def _balanced_json(text: str, start_at: int = 0) -> dict:
    """Extract + parse the first balanced {...} object at/after start_at.
    Brace-counting (string-aware) so it survives code fences, trailing
    junk, pretty-printed/multiline JSON, and braces inside strings.
    Returns {} when none parses."""
    start = text.find("{", start_at)
    if start < 0:
        return {}
    scanner = _BraceScanner()
    for i in range(start, len(text)):
        if scanner.feed(text[i]):
            try:
                return json.loads(text[start:i + 1])
            except (ValueError, TypeError):
                return {}
    return {}


_REASONING_PREFIX_RE = re.compile(
    r"^[ \t]*(?:THOUGHT|THINK|THINKING|REASONING|ANALYSIS|PLAN|ACTION|FINAL)"
    r"[ \t]*:[ \t]*(?:FINAL\b[ \t]*)?",
    re.IGNORECASE)


def _strip_reasoning_prefix(text: str) -> str:
    """Strip a leaked chain-of-thought marker (``THOUGHT:``/``REASONING:`` …)
    from the START of a final answer. A local model sometimes emits its
    reasoning line as the answer (or a `FINAL:` whose text begins with
    `THOUGHT:`), so the user saw ``THOUGHT: The user asked me to…`` instead of
    the plan/answer. Only strips a LEADING marker; reasoning that legitimately
    appears mid-answer is untouched."""
    if not text:
        return text
    t = text.lstrip()
    m = _REASONING_PREFIX_RE.match(t)
    if not m:
        return text
    rest = t[m.end():]
    # Drop only the first reasoning line; keep everything after it. If the whole
    # thing was one reasoning line with nothing useful after, keep it (better a
    # thought than an empty answer).
    nl = rest.find("\n")
    tail = rest[nl + 1:].lstrip() if nl != -1 else ""
    return tail or rest.strip() or text


# A model that fumbles the ACTION/ARGS_JSON protocol can leak scaffolding INTO an
# answer we then surface raw to the UI — the user saw a bare ``ARGS_JSON: null``
# (or ``ACTION:``, ``{}``) as the reply. Strip marker-ONLY lines from any text
# treated as a final answer. Conservative by design: a keyword line survives
# when it carries real content (``ACTION: build``), so code/prose and YAML like
# ``status: open`` are untouched — only bare / null / empty-object markers go.
_PROTOCOL_NOISE_RE = re.compile(
    r"(?im)^[ \t]*(?:action|args_json|ask|final|thought|reasoning)"
    r"[ \t]*:?[ \t]*(?:null|none|\{\s*\})?[ \t]*$")


# The completion pseudo-tool line itself (`ACTION: FINAL`). _PROTOCOL_NOISE_RE
# deliberately KEEPS an `ACTION: <name>` line, because a name is content — but
# inside the completion branch that name IS the marker, so the line is noise
# there and nowhere else.
_COMPLETION_MARKER_RE = re.compile(
    r"(?im)^[ \t>#*_`]*action[ \t]*:?[ \t]*"
    r"(?:final|finish|done|complete|final_answer)[ \t.,:;!*_`]*\r?$")


# A labelled reasoning line, for the single-line `REASONING:` spelling that
# _THOUGHT_RE (THOUGHT-only, DOTALL) does not cover. Both are applied ONLY to
# the whole-turn fallback — never to text the model wrote after the marker,
# which is the answer itself.
_REASONING_LINE_RE = re.compile(r"(?im)^[ \t]*reasoning[ \t]*:.*$")


def _strip_protocol_noise(text: str) -> str:
    """Remove leaked protocol marker-only lines from a would-be final answer."""
    return _PROTOCOL_NOISE_RE.sub("", text or "").strip()


_COMPLETION_TOOL_NAMES = ("final", "finish", "done", "complete", "final_answer")
_ARGS_JSON_BLOB_RE = re.compile(r"(?is)ARGS_JSON\s*+:?\s*+\{.*\}")


def _empty_final(out: str) -> dict:
    """The continue-nudge for a marker with no answer behind it.

    `or txt.strip()` used to hand the marker itself back as the answer, so a
    model that emitted a bare `ACTION: FINAL` ended the turn with the literal
    words "ACTION: FINAL" in the chat — after doing all the work. Worse,
    `THOUGHT: …\nACTION: FINAL` published the model's private reasoning as the
    reply. An empty completion is not an answer.
    """
    tho = _THOUGHT_RE.search(out)
    return {"kind": "continue", "reason": "empty_final",
            "thought": tho.group(1).strip() if tho else ""}


def _text_from_args(fargs) -> str:
    """The answer a completion pseudo-tool carried in its args."""
    if not isinstance(fargs, dict):
        return ""
    txt = str(fargs.get("text") or fargs.get("answer")
              or fargs.get("response") or fargs.get("content")
              or fargs.get("output") or fargs.get("message")
              or fargs.get("result") or fargs.get("summary") or "")
    if txt.strip():
        return txt
    # Unrecognized key (model invented an arg name) — take the longest string
    # value rather than leak the raw ARGS_JSON blob.
    strs = [v for v in fargs.values() if isinstance(v, str) and v.strip()]
    return max(strs, key=len) if strs else ""


def _clean_whole_turn(txt: str) -> str:
    """Strip the marker and the model's THOUGHT lines from a WHOLE-TURN slice.

    Only that slice contains them: the after-the-marker slice is the user's
    answer verbatim, and running these over it ate `thought:` / `action: done`
    lines out of a YAML block the answer was quoting.
    """
    # Same ARGS_JSON removal the after-slice gets: a completion whose args
    # carried no usable text (`{"text": ""}`) otherwise published the raw args
    # blob as the answer.
    body = _ARGS_JSON_BLOB_RE.sub("", txt)
    body = _COMPLETION_MARKER_RE.sub("", body)
    # Whole BLOCK, not the first line: a multi-line thought left its tail
    # behind, which is the model's private reasoning published as the reply —
    # the very bug being fixed.
    body = _THOUGHT_RE.sub("", body)
    return _REASONING_LINE_RE.sub("", body)


def _completion_pseudo_tool(out: str, act) -> dict:
    """Some models emit the completion as a fake TOOL call —
    `ACTION: final ARGS_JSON: {"text": "…"}` — instead of the `FINAL:` marker.
    Dispatching that hits "unknown tool: final" and the model loops, so coerce
    it into a real final answer."""
    m2 = re.search(r"ARGS_JSON\s*:?", out, re.IGNORECASE)
    txt = _text_from_args(_balanced_json(out, m2.end() if m2 else act.end()))
    # No usable JSON args — the answer is the plain text the model wrote AFTER
    # the `ACTION: FINAL` marker (its reasoning sits ABOVE it). Fall back to
    # that slice, NOT the whole turn, or the thought + marker leak into the
    # answer and break a skill's "nothing else" format. Only use the whole turn
    # as a last resort (marker at EOF). Strip any ARGS_JSON {...} blob from the
    # slice so a fumbled/unknown args shape never surfaces raw protocol.
    whole_turn = False
    if not txt.strip():
        txt = _ARGS_JSON_BLOB_RE.sub("", out[act.end():]).strip()
        if not txt:
            txt, whole_turn = out.strip(), True
    if whole_turn:
        cleaned = _strip_protocol_noise(_clean_whole_turn(txt))
    else:
        cleaned = txt.strip()
    # What survives a dressed-up marker is its dressing: `**ACTION: FINAL**`
    # leaves `**`, `ACTION: FINAL.` leaves `.`. An answer with no letter or
    # digit anywhere in it is not an answer — whichever slice it came from.
    if cleaned and not re.search(r"\w", cleaned):
        cleaned = ""
    return {"kind": "final", "text": cleaned} if cleaned else _empty_final(out)


def _action_step(out: str, act, name: str) -> dict:
    """A real tool call: its args are the first balanced {...} after the
    ARGS_JSON marker if present, else after the ACTION line (```json fenced
    args included)."""
    m = re.search(r"ARGS_JSON\s*:?", out, re.IGNORECASE)
    args = _balanced_json(out, m.end() if m else act.end())
    said = _take_description(name, args)
    # Inline-args rescue (dspy A/B finding, verified 6/6 on the NUC): local
    # models sometimes emit `ACTION: tool {"item": "x"}` followed by an EMPTY
    # `ARGS_JSON: {}` — the marker's {} shadowed the good inline object and the
    # tool ran arg-less forever (deterministic at temp 0). When the marker slot
    # parsed empty but a non-empty balanced object sits right after the ACTION
    # name, use the inline object.
    if not args and m:
        args = _balanced_json(out, act.end()) or args
        said = _take_description(name, args) or said
    thought = _THOUGHT_RE.search(out)
    step = {"kind": "action", "tool": name, "args": args,
            "thought": thought.group(1).strip() if thought else ""}
    if said:
        step["description"] = said
    return step


def _real_description_arg(name: str) -> bool:
    """The tool takes ``description`` as an input of its own (``jira_create``'s
    issue body, a skill's charter) — then it is not words for the user."""
    try:
        from ._tools._schemas import CATALOG
        entry = CATALOG.get(name)
    except Exception:  # noqa: BLE001
        return True
    if not entry:
        return True              # unknown shape: leave its args alone
    if name == "run_command":
        return False
    return "description" in (entry[1] or {})


def _take_description(name: str, args) -> str:
    """Take the user-facing description off ``args`` (in place) and return it.
    run_command offers it; a model that adds one to another tool's call gets
    it taken off too, unless that tool has a real ``description`` input. Off
    the args, every guard, signature and repeat check sees the call exactly as
    before; the description only travels with the step event."""
    if not isinstance(args, dict) or "description" not in args or _real_description_arg(name):
        return ""
    said = args.pop("description", None)
    if not isinstance(said, str):
        return ""
    return " ".join(said.split())[:160]


def _scaffolding_only(out: str) -> bool:
    """ONLY leaked protocol scaffolding (a lone `ARGS_JSON: null`, a bare
    `ACTION:` with no tool, an empty `{}`/```json fence). A local model
    sometimes emits a tool call with no ACTION line; the prose fallback then
    surfaced raw "ARGS_JSON: null" to the UI as the answer."""
    return not re.sub(
        r"(?im)^\s*(?:action|args_json|final|ask|thought|reasoning)\b\s*:?.*$"
        r"|^\s*(?:null|\{\s*\}|```+\w*|```+)\s*$",
        "", out).strip().strip("`").strip()


def _known_tool(name: str) -> bool:
    try:
        from ._registry import TOOLS
        return name in TOOLS
    except Exception:  # noqa: BLE001 — registry import must never break parsing
        return False


def _credible_action(out: str):
    """The first ``ACTION:`` match that is really a tool call, or None.

    The marker regex is case-insensitive and unanchored, so a native-mode reply
    that is plain prose — "Recommended action: the config must…" — used to be
    dispatched as a call to a tool named ``the`` ("unknown tool: the" in the
    chat). A match counts when the name is a registered tool or a completion
    pseudo-tool (anywhere, as before), or when it is written as a protocol
    line: ``ACTION:`` at the start of a line and the name followed by the end
    of the line, ``{`` or ``ARGS_JSON``. A deliberate call to a tool that does
    not exist (``ACTION: web_search`` + ARGS_JSON) still dispatches, so the
    model gets told why; prose falls through to the answer."""
    for m in _ACTION_RE.finditer(out):
        name = m.group(1)
        if name.lower() in _COMPLETION_TOOL_NAMES or _known_tool(name):
            return m
        line_head = out[out.rfind("\n", 0, m.start()) + 1:m.start()]
        if (not re.sub(r"[\s>*_`#-]", "", line_head)
                and re.match(r"[ \t*_`]*(?:$|\n|\{|ARGS_JSON)", out[m.end():],
                             re.IGNORECASE)):
            return m
    return None


_NARRATED_RE = re.compile(r"^\s*(?:\W{0,3})Called\s+([A-Za-z_]\w*)\s*\(", re.S)


_NARRATED_MAX = 20_000
#: A fabricated result the model tacks on after the call ("Result of x:\n…").
_NARRATED_TAIL = re.compile(r"^(?:Result of\b.*)$", re.S | re.I)


def _balanced(text: str, start: int) -> int:
    """Index just past the brace/bracket group that opens at ``start``, or -1.
    Quotes are respected so a brace inside a string does not count."""
    depth, quote, esc = 0, "", False
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def narrated_call(text: str) -> "tuple[str, dict] | None":
    """A reply that WRITES a tool call as prose — ``Called run_command({"cmd": …})``
    — instead of making it. That is how earlier calls look in the history when a
    server could not take the native replay (``flatten_tool_messages``), and the
    model copies the shape: the turn then ended with that line as its "answer"
    and the command never ran. Returns ``(tool, args)`` for the first such call
    when the tool exists and the arguments parse (JSON, or a Python-style dict),
    else None."""
    m = _NARRATED_RE.match(text or "")
    if not m:
        return None
    name = m.group(1)
    try:
        from ._registry import TOOLS
        if name not in TOOLS:
            return None
    except Exception:  # noqa: BLE001 — unknown registry: do not guess
        return None
    at = m.end()
    while at < len(text) and text[at] in " \t\n":
        at += 1
    if at >= len(text) or text[at] != "{":
        return None
    end = _balanced(text, at)
    if end < 0 or end - at > _NARRATED_MAX:
        return None
    # The call must be the WHOLE reply (bar the closing paren and any made-up
    # "Result of …" block the model appended). A reply that goes on to explain —
    # "Called run_command({...}) to build it, then …" — is the model REPORTING
    # what it ran, and running it again would repeat a command it only described.
    rest = text[end:].strip().lstrip(")").strip()
    rest = _NARRATED_TAIL.sub("", rest).strip()
    if rest and not rest.startswith(";"):
        return None
    raw = text[at:end]
    try:
        args = json.loads(raw)
    except Exception:  # noqa: BLE001 — incl. RecursionError on hostile nesting
        try:
            import ast
            args = ast.literal_eval(raw)
        except Exception:  # noqa: BLE001
            return None
    return (name, args) if isinstance(args, dict) else None


def _broken_narrated_call(text: str) -> str:
    """The tool name when ``text`` opens with ``Called <tool>(`` for a real
    tool but is no call that can be run (cut off mid-arguments, invalid
    JSON), else "". A reply that reports a call and goes on to explain is
    not this: it has prose after a complete call."""
    m = _NARRATED_RE.match(text or "")
    if not m:
        return ""
    name = m.group(1)
    try:
        from ._registry import TOOLS
        if name not in TOOLS:
            return ""
    except Exception:  # noqa: BLE001
        return ""
    at = m.end()
    while at < len(text) and text[at] in " \t\n":
        at += 1
    if at >= len(text) or text[at] != "{":
        return ""
    end = _balanced(text, at)
    if end < 0:
        return name                  # cut off: the arguments never close
    rest = text[end:].strip().lstrip(")").strip()
    if rest and not _NARRATED_TAIL.match(rest) and not rest.startswith(";"):
        return ""                    # it reports a call and explains: an answer
    raw = text[at:end]
    try:
        json.loads(raw)
        return ""                    # a complete call (with prose after it)
    except Exception:  # noqa: BLE001
        try:
            import ast
            ast.literal_eval(raw)
            return ""
        except Exception:  # noqa: BLE001
            return name


def _parse(out: str) -> dict:
    """Parse a model turn into {kind, ...}. Tolerant of code fences,
    pretty-printed JSON, and stray markdown around the protocol."""
    act = _credible_action(out)
    if not act:
        # "Called run_command({...})" written as prose: run it, do not publish it.
        told = narrated_call(out)
        if told is not None:
            return _parse(f"ACTION: {told[0]}\nARGS_JSON: "
                          f"{json.dumps(told[1], ensure_ascii=False)}")
        broken = _broken_narrated_call(out)
        if broken:
            # A call written as text that is cut off or not valid JSON: it did
            # not run, and it is not an answer either.
            return {"kind": "continue", "reason": "broken_call", "tool": broken,
                    "thought": ""}
    # Prefer ACTION when present (models sometimes mention "final" in prose).
    if act:
        name = act.group(1).strip()
        if name.lower() in _COMPLETION_TOOL_NAMES:
            return _completion_pseudo_tool(out, act)
        return _action_step(out, act, name)
    ask = _ASK_RE.search(out)
    if ask:
        return {"kind": "ask", "text": ask.group(1).strip()}
    fin = _FINAL_RE.search(out)
    if fin:
        txt = _strip_protocol_noise(fin.group(1))
        # `FINAL:` present but only scaffolding after it (e.g. `FINAL:\nARGS_JSON:
        # null`) → don't answer with garbage; fall through to the continue-nudge.
        return {"kind": "final", "text": txt} if txt else _empty_final(out)
    # No FINAL/ASK/ACTION marker. If the model was mid-reasoning — it emitted a
    # THOUGHT (intent to act) but no ACTION — it almost certainly got truncated
    # or forgot to emit the ACTION line. Treating that as the final answer stops
    # the run early ("Now I need to create the script… Let me first check…" then
    # nothing). Signal CONTINUE so the loop nudges it to act instead of ending.
    tho = _THOUGHT_RE.search(out)
    if tho:
        return {"kind": "continue", "thought": tho.group(1).strip() or out.strip()}
    if _scaffolding_only(out):
        # Nudge the model to re-emit a proper step instead of quitting on garbage.
        return {"kind": "continue",
                "thought": "malformed step (protocol scaffolding only) — "
                           "re-emit a valid ACTION + ARGS_JSON, or FINAL: <answer>"}
    # Genuinely just prose with no protocol at all → treat as the final answer.
    # Tag it IMPLICIT (no explicit ``FINAL:`` marker): in interactive chat that's
    # the real answer, but in a work-producing run (doer / builder) it's usually
    # premature narration ("let me test what's happening…") and the loop should
    # nudge-and-continue rather than quit — see the ``final`` branch in the loop.
    return {"kind": "final",
            "text": _strip_protocol_noise(out) or out.strip(), "implicit": True}


