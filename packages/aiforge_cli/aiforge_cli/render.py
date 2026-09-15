"""Events in, terminal text out — and nothing else.

This module is pure on purpose: no printing, no cursor control, no clock it
did not get handed. :class:`Renderer` turns one event into an :class:`Op`
describing what to commit to the scrollback and what the single redrawn status
line should say; the caller owns the terminal. That is what makes the whole
rendering layer testable from a recorded event list with no tty at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .colors import Palette

MAX_ARGS = 72                 # middle-ellipsis budget for a tool's arguments
MAX_DIGEST = 96
DIFF_LINES = {-1: 0, 0: 14, 1: 400}


@dataclass
class Op:
    """What the caller should do with one event."""

    lines: list[str] = field(default_factory=list)   # commit to scrollback
    tail: str | None = None                          # replace the status line
    stream: str = ""                                 # append inline, no newline
    approval: dict[str, Any] | None = None           # needs an answer now
    finished: bool = False                           # the turn is over


def _ellipsize(text: str, budget: int) -> str:
    """Shorten in the MIDDLE. A truncated tail hides the end of a path, which
    is the half that says which file this was."""
    text = " ".join(text.split())
    if len(text) <= budget:
        return text
    keep = budget - 1
    head = keep // 2
    tail = keep - head
    return text[:head] + "…" + text[-tail:]


def _fmt_args(args: Any, budget: int) -> str:
    if not isinstance(args, dict):
        return _ellipsize(str(args or ""), budget)
    parts = []
    for key, value in args.items():
        if isinstance(value, str):
            shown = value
        else:
            shown = json.dumps(value, default=str)
        parts.append(f"{key}={shown}")
    return _ellipsize("  ".join(parts), budget)


def _fmt_duration(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return ""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s"


def _digest(result: Any) -> str:
    """One line for what a tool returned.

    The full result is available with -v; the default view needs the part a
    human scans for — did it work, and how much did it touch.
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return _ellipsize(result, MAX_DIGEST)
    if not isinstance(result, dict):
        return _ellipsize(str(result), MAX_DIGEST)
    for key in ("error", "detail"):
        if result.get(key):
            return _ellipsize(str(result[key]), MAX_DIGEST)
    bits: list[str] = []
    if "additions" in result or "deletions" in result:
        bits.append(f"+{result.get('additions', 0)} -{result.get('deletions', 0)}")
    for key, label in (("lines", "lines"), ("bytes", "bytes"), ("files", "files"),
                       ("count", "hits"), ("exit_code", "exit")):
        if isinstance(result.get(key), int):
            bits.append(f"{result[key]} {label}" if label != "exit"
                        else f"exit {result[key]}")
    if not bits:
        for key in ("path", "file", "summary", "stdout", "output", "message"):
            if result.get(key):
                bits.append(str(result[key]).strip().splitlines()[0] if key in
                            ("stdout", "output", "summary", "message") else str(result[key]))
                break
    return _ellipsize("  ".join(bits) if bits else "ok", MAX_DIGEST)


def _diff_lines(text: str, pal: Palette, budget: int) -> list[str]:
    out: list[str] = []
    raw = text.splitlines()
    for line in raw[:budget] if budget else []:
        if line.startswith("+"):
            out.append("  " + pal(line, "plus"))
        elif line.startswith("-"):
            out.append("  " + pal(line, "minus"))
        elif line.startswith("@@"):
            out.append("  " + pal(line, "hunk"))
        else:
            out.append("  " + pal(line, "dim"))
    if budget and len(raw) > budget:
        out.append("  " + pal(f"… {len(raw) - budget} more diff lines (-v for all)", "dim"))
    return out


class Renderer:
    """Per-turn rendering state.

    One instance spans a whole session; :meth:`begin_turn` resets what is
    per-turn, and :meth:`begin_replay` marks the next events as a re-delivery
    of ones we may already have printed (an /attach after a dropped stream), so
    the transcript never doubles up.
    """

    def __init__(self, pal: Palette, *, verbosity: int = 0):
        self.pal = pal
        self.verbosity = verbosity
        self._tool: dict[str, Any] | None = None
        self._streamed = ""
        self._replayed = ""
        self._emitted = 0
        self._answered = False
        self._seen: set[str] = set()
        self._replay = False
        self._tools = 0
        self._failed = 0
        self._usage: dict[str, Any] = {}
        self._elapsed: float | None = None

    # ── turn boundaries ────────────────────────────────────────────────────

    def begin_turn(self) -> None:
        self._tool = None
        self._streamed = ""
        self._replayed = ""
        self._emitted = 0
        self._answered = False
        self._seen.clear()
        self._replay = False
        self._tools = 0
        self._failed = 0
        self._usage = {}
        self._elapsed = None

    def begin_replay(self) -> None:
        self._replay = True

    def user_line(self, text: str) -> str:
        return f"{self.pal('▸', 'user')} {text}"

    # ── the dispatch ───────────────────────────────────────────────────────

    def handle(self, event: dict[str, Any]) -> Op:
        kind = str(event.get("type") or "")
        fn = getattr(self, f"_on_{kind}", None)
        if fn is None:
            return self._on_unknown(kind, event)
        return fn(event)

    # ── individual events ──────────────────────────────────────────────────

    def _on_attached(self, ev: dict[str, Any]) -> Op:
        if not ev.get("running"):
            return Op()
        self.begin_replay()
        return Op(tail=self._tail("re-attached to the run in progress"))

    def _on_ping(self, _ev: dict[str, Any]) -> Op:
        return Op(tail=self._tail())

    def _on_thought(self, ev: dict[str, Any]) -> Op:
        text = str(ev.get("text") or "").strip()
        if not text or self._dup(f"thought:{text}"):
            return Op()
        if self.verbosity < 0:
            return Op(tail=self._tail(_ellipsize(text, 60)))
        return Op(lines=[f"{self.pal('●', 'thought')} {self.pal(text, 'thought')}"],
                  tail=self._tail())

    def _on_tool_start(self, ev: dict[str, Any]) -> Op:
        self._tool = {"name": ev.get("name") or "tool", "args": ev.get("args")}
        label = f"{self._tool['name']}  {_fmt_args(self._tool['args'], MAX_ARGS)}".rstrip()
        return Op(tail=self._tail(label))

    def _on_tool(self, ev: dict[str, Any]) -> Op:
        name = str(ev.get("name") or (self._tool or {}).get("name") or "tool")
        key = f"tool:{ev.get('call_id', '')}:{name}:{self._tools}"
        if self._dup(key):
            return Op()
        pending = self._tool or {}
        self._tool = None
        self._tools += 1
        result = ev.get("result")
        failed = bool(isinstance(result, dict) and (result.get("error") or result.get("ok") is False))
        self._failed += 1 if failed else 0
        mark = self.pal("✗", "fail") if failed else self.pal("✓", "ok")
        args = _fmt_args(ev.get("args") if ev.get("args") is not None
                         else pending.get("args"),
                         MAX_ARGS if self.verbosity <= 0 else 400)
        dur = _fmt_duration(ev.get("duration_s") or ev.get("elapsed_s"))
        digest = _digest(result)
        line = f"{mark} {self.pal(name, 'head')}  {args}"
        trailer = "  ".join(x for x in (digest, dur) if x)
        if trailer:
            line += f"   {self.pal(trailer, 'dim' if not failed else 'fail')}"
        lines = [line]
        if self.verbosity < 0:
            lines = []
        diff = _diff_text(result)
        if diff and self.verbosity >= 0:
            lines += _diff_lines(diff, self.pal, DIFF_LINES[min(max(self.verbosity, -1), 1)])
        if self.verbosity > 0 and isinstance(result, dict):
            lines += ["  " + self.pal(l, "dim")
                      for l in json.dumps(result, indent=2, default=str).splitlines()[:60]]
        return Op(lines=lines, tail=self._tail())

    def _on_delta(self, ev: dict[str, Any]) -> Op:
        text = str(ev.get("text") or ev.get("delta") or "")
        if not text:
            return Op()
        if self._replay:
            # A replay re-sends the answer from the start of the run. Deltas
            # concatenate, so the part still owed to the screen is whatever
            # sits past the character count already printed.
            self._replayed += text
            self._streamed = self._replayed
            if len(self._replayed) <= self._emitted:
                return Op()
            fresh = self._replayed[self._emitted:]
            self._emitted = len(self._replayed)
            return Op(stream=fresh if self.verbosity >= 0 else "")
        self._streamed += text
        if self.verbosity < 0:
            return Op()
        self._emitted += len(text)
        return Op(stream=text)

    def _on_message(self, ev: dict[str, Any]) -> Op:
        text = str(ev.get("text") or "").rstrip()
        if not text or self._dup(f"message:{hash(text)}"):
            return Op()
        streamed = self._streamed.strip()
        self._answered = True
        if streamed and text.strip() == streamed:
            return Op(lines=[""], tail=self._tail())       # already on screen
        if streamed and text.strip().startswith(streamed):
            return Op(lines=[text.strip()[len(streamed):], ""], tail=self._tail())
        return Op(lines=[*_answer_lines(text, self.pal), ""], tail=self._tail())

    def _on_usage(self, ev: dict[str, Any]) -> Op:
        self._usage = {k: v for k, v in ev.items() if k != "type"}
        return Op(tail=self._tail())

    def _on_approval(self, ev: dict[str, Any]) -> Op:
        rule = self.pal("── approval ──────────────────────────────", "approval")
        lines = [rule]
        tool = ev.get("tool") or ev.get("name") or "action"
        lines.append(f"  {self.pal(str(tool), 'head')}  "
                     f"{_fmt_args(ev.get('args'), 200)}")
        preview = ev.get("preview") or ev.get("diff") or ev.get("command")
        if isinstance(preview, str) and preview.strip():
            lines += _diff_lines(preview, self.pal, DIFF_LINES[1])
        if ev.get("reason"):
            lines.append("  " + self.pal(str(ev["reason"]), "dim"))
        return Op(lines=lines, approval=ev, tail=None)

    def _on_approval_expired(self, _ev: dict[str, Any]) -> Op:
        return Op(lines=[self.pal("✗ approval timed out — the tool was rejected", "fail")])

    def _on_auto_approved(self, ev: dict[str, Any]) -> Op:
        if self.verbosity < 1:
            return Op()
        return Op(lines=[self.pal(f"⊙ auto-approved {ev.get('tool') or ''}", "dim")])

    def _on_captured(self, ev: dict[str, Any]) -> Op:
        text = _ellipsize(str(ev.get("text") or ""), 80)
        cat = ev.get("category") or "rule"
        return Op(lines=[f"{self.pal('⊕', 'ok')} {cat} captured: "
                         f"{self.pal(text, 'dim')}"], tail=self._tail())

    def _on_subtasks(self, ev: dict[str, Any]) -> Op:
        items = ev.get("subtasks") or ev.get("items") or []
        if not isinstance(items, list) or not items:
            return Op()
        head = self.pal(f"⋮ plan · {len(items)} subtasks", "head")
        lines = [head]
        for item in items[:12]:
            goal = item.get("goal") or item.get("title") or item.get("slug") if isinstance(item, dict) else str(item)
            lines.append("  " + self.pal(f"– {_ellipsize(str(goal), 88)}", "dim"))
        return Op(lines=lines, tail=self._tail())

    def _on_subtask_update(self, ev: dict[str, Any]) -> Op:
        if self.verbosity < 0:
            return Op()
        slug = ev.get("slug") or ""
        status = ev.get("status") or ""
        role = "ok" if status in ("done", "completed") else "running"
        return Op(lines=[f"{self.pal('·', role)} {slug} {self.pal(str(status), 'dim')}"],
                  tail=self._tail())

    def _on_stage_start(self, ev: dict[str, Any]) -> Op:
        return Op(tail=self._tail(f"{ev.get('name') or 'stage'}…"))

    def _on_stage_done(self, ev: dict[str, Any]) -> Op:
        if self.verbosity < 1:
            return Op(tail=self._tail())
        return Op(lines=[self.pal(f"▪ {ev.get('name') or 'stage'} done", "dim")])

    def _on_plan_ready(self, ev: dict[str, Any]) -> Op:
        path = ev.get("path") or ev.get("spec") or ""
        return Op(lines=[f"{self.pal('✓', 'ok')} plan ready {self.pal(str(path), 'dim')}"])

    def _on_ticket(self, ev: dict[str, Any]) -> Op:
        return Op(lines=[f"{self.pal('✓', 'ok')} ticket {ev.get('id') or ''} created"])

    def _on_stopped(self, _ev: dict[str, Any]) -> Op:
        return Op(lines=[self.pal("■ stopped", "warn")], finished=True)

    def _on_error(self, ev: dict[str, Any]) -> Op:
        msg = str(ev.get("text") or ev.get("error") or "unknown error").strip()
        lines = [f"{self.pal('✗ error', 'error')} {msg}"]
        lines.append(self.pal("  aiforge box logs --tail 50   shows what the sandbox saw",
                              "dim"))
        return Op(lines=lines, finished=False)

    def _on_builder_done(self, ev: dict[str, Any]) -> Op:
        return Op(lines=[f"{self.pal('✓', 'ok')} {ev.get('kind') or 'builder'} saved"])

    def _on_done(self, ev: dict[str, Any]) -> Op:
        self._elapsed = ev.get("elapsed_s") or self._elapsed
        bits = [_fmt_duration(self._elapsed)] if self._elapsed else []
        bits.append(f"{self._tools} tool{'s' if self._tools != 1 else ''}")
        if self._failed:
            bits.append(self.pal(f"{self._failed} failed", "fail"))
        pct = _pct(self._usage)
        if pct is not None:
            bits.append(f"ctx {pct:.0f}%")
        line = self.pal("done", "ok") + "  " + self.pal(" · ".join(b for b in bits if b), "dim")
        return Op(lines=[line], finished=True)

    def _on_unknown(self, kind: str, ev: dict[str, Any]) -> Op:
        """An event this binary has not heard of.

        The backend ships faster than the installed CLI does, so a new event
        type is normal. One dim line keeps it visible without pretending to
        understand it — and never raises mid-run.
        """
        if self.verbosity < 1 or not kind:
            return Op()
        return Op(lines=[self.pal(f"· {kind} {_ellipsize(json.dumps(ev, default=str), 100)}",
                                  "dim")])

    # ── the status line ────────────────────────────────────────────────────

    def _tail(self, action: str | None = None) -> str:
        if self.verbosity < 0:
            return ""
        parts = []
        if action:
            parts.append(action)
        elif self._tool:
            parts.append(str(self._tool["name"]))
        pct = _pct(self._usage)
        if pct is not None:
            parts.append(self.pal(f"ctx {pct:.0f}%", self.pal.ctx(pct)))
        req = self._usage.get("llmSession") or self._usage.get("llmTurn")
        if req:
            parts.append(self.pal(f"req {req}", "dim"))
        parts.append(self.pal("esc stop", "dim"))
        return "  ".join(parts)

    def _dup(self, key: str) -> bool:
        """True when a replay is re-delivering something already printed."""
        if key in self._seen:
            return True
        self._seen.add(key)
        return False


def _pct(usage: dict[str, Any]) -> float | None:
    value = usage.get("pct")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _diff_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    for key in ("diff", "patch", "preview"):
        value = result.get(key)
        if isinstance(value, str) and ("\n" in value or value.startswith(("+", "-", "@@"))):
            return value
    return ""


def _answer_lines(text: str, pal: Palette) -> list[str]:
    """The final answer, lightly styled.

    Not a markdown renderer: headings, bullets, inline code and fenced blocks
    are the four things that actually show up in an answer, and anything more
    would fight the user's own pager.
    """
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            lang = line[3:].strip()
            out.append(pal(f"┌ {lang}" if fenced and lang else ("┌" if fenced else "└"), "dim"))
            continue
        if fenced:
            out.append(pal("│ ", "dim") + pal(line, "code"))
            continue
        if line.startswith("#"):
            out.append(pal(line.lstrip("# ").strip(), "head"))
            continue
        if line.lstrip().startswith(("- ", "* ")):
            indent = line[:len(line) - len(line.lstrip())]
            out.append(f"{indent}{pal('•', 'code')} {line.lstrip()[2:]}")
            continue
        out.append(_inline_code(line, pal))
    return out


def _inline_code(line: str, pal: Palette) -> str:
    if "`" not in line:
        return line
    chunks = line.split("`")
    return "".join(c if i % 2 == 0 else pal(c, "code") for i, c in enumerate(chunks))
