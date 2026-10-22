"""An answer from a turn whose commands broke one of the user's rules.

The user's rules ("run code and tests only on the VM", "databases only in
Docker") are in every prompt, and the model reads them. Then the way the rule
asks for fails — the VM does not answer — and the model quietly does what the
rule forbids (``python -m pytest`` on this machine) and reports "all tests
pass". The rule was in front of it; it was not followed.

So, at FINAL, when the user has rules and the turn ran commands, one short
model call checks the turn's commands against the rules. A command that does
what a rule forbids sends the model back once: the result does not count;
follow the rule, or tell the user what blocks it. Still broken at the next
FINAL: the answer starts with a note naming the command and the rule.

Fails open: no rules, no commands, the check call failing or timing out —
the answer goes out as it is. ``AIFORGE_CHAT_RULE_CHECK=0`` turns it off.
"""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("aiforge.rule_check")

_MAX_ACTIONS = 30
_MAX_RULES_CHARS = 3000

_SYSTEM = (
    "You check an AI coding agent's shell commands against the rules its user "
    "set. A command BREAKS a rule only when it plainly does what the rule "
    "forbids (runs locally what a rule says must run on a VM or remote host; "
    "installs or starts a database outside Docker when the rule says Docker; "
    "uses a tool, branch, account or place the rule forbids). Trying to follow "
    "a rule and failing (an ssh that could not connect) is NOT a break. "
    "Reading files, listing, git status and the like break nothing unless a "
    "rule says so. Reply with ONLY a JSON array: "
    '[{"action": <number>, "rule": "<the rule it breaks, in a few words>"}] '
    "— [] when no command breaks a rule.")


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_RULE_CHECK", "1").strip().lower() \
        not in ("0", "false", "no", "off")


_BLOCKED = re.compile(
    r"\b(?:could\s*n[o']t|cannot|can't|unable|not\s+able|blocked|unreachable|"
    r"did\s+not\s+run|didn't\s+run|not\s+run|failed\s+to)\b", re.IGNORECASE)


def _says_blocked(text: str) -> bool:
    """The answer says the work could not be done as the rule asks."""
    return bool(_BLOCKED.search(text or ""))


def _commands(session_id, since: int = 0) -> list[str]:
    """The shell commands this turn ran (after the first ``since``)."""
    try:
        from aiforge_core.runtime import action_log, shell_writes
        steps = action_log.live_steps(session_id) or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for s in steps:
        name = str(s.get("name") or "")
        args = s.get("args") if isinstance(s.get("args"), dict) else {}
        if name in shell_writes.SHELL_TOOLS:
            cmd = shell_writes.command_of(args)
            res = s.get("result") if isinstance(s.get("result"), dict) else {}
            if cmd and not res.get("blocked"):
                out.append(cmd)
    return out[since:][-_MAX_ACTIONS:]


def _parse(raw: "str | None") -> "list | None":
    if not raw:
        return None
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return None
    try:
        got = json.loads(m.group(0))
    except (TypeError, ValueError):
        return None
    return got if isinstance(got, list) else None


def breaks(role: str, rules: list, commands: list, session_id) -> list:
    """``[(command, rule)]`` the check finds; ``[]`` when none or it failed."""
    if not rules or not commands:
        return []
    from .._turn._plan_llm import _ask
    rules_text = "\n".join(rules)[:_MAX_RULES_CHARS]
    acts = "\n".join(f"{i}. {' '.join(c.split())[:300]}" for i, c in enumerate(commands, 1))
    raw = _ask(role, _SYSTEM, f"THE USER'S RULES:\n{rules_text}\n\nTHE COMMANDS THIS "
               f"TURN RAN:\n{acts}", session_id, max_tokens=400)
    out = []
    for item in _parse(raw) or []:
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get("action"))
        except (TypeError, ValueError):
            continue
        rule = " ".join(str(item.get("rule") or "").split())[:200]
        if 1 <= n <= len(commands) and rule:
            pair = (commands[n - 1], rule)
            if pair not in out:
                out.append(pair)
    return out


class RuleBreakGuard:
    """A command of this turn did what one of the user's rules forbids."""

    counter = "rule_break_nudges"
    strip_body = False
    echo_text = False

    def __init__(self, cwd, readonly_mode, builder, plan_mode):
        self.cwd = cwd
        self.off = bool(readonly_mode or builder or plan_mode) or not enabled()
        self._st = None
        self._last: list = []

    def applies(self, st) -> bool:
        if self.off or getattr(st, "session_id", None) is None:
            log.debug("rule check off (readonly/builder/plan or no session)")
            return False
        self._st = st
        return True

    def budget(self, st) -> int:
        return 1

    def detect(self, text: str) -> list:
        st = self._st
        if st is None:
            return []
        try:
            from .._tools._memory import user_rule_lines
            rules = user_rule_lines(str(self.cwd or ""),
                                    session_id=getattr(st, "session_id", None))
            since = int(getattr(st, "rule_check_mark", 0) or 0)
            cmds = _commands(st.session_id, since)
            pending = getattr(st, "rule_breaks_pending", None) or []
            if pending and not cmds:
                # Sent back, ran nothing new, and answers again: unless the
                # answer now says the work is blocked, it still stands on
                # the commands that broke the rule.
                return [] if _says_blocked(text) else list(pending)
            key = hash((tuple(rules), tuple(cmds)))
            cache = getattr(st, "rule_check_cache", None)
            if cache is None:
                cache = st.rule_check_cache = {}
            if key in cache:
                return list(cache[key])
            found = breaks(getattr(st, "role", "chat") or "chat", rules, cmds,
                           st.session_id)
            cache[key] = found
        except Exception as exc:  # noqa: BLE001 — a check never blocks the answer
            log.warning("rule check skipped: %s", exc)
            return []
        log.info("rule check: %d rule line(s), %d command(s), %d break(s)",
                 len(rules), len(cmds), len(found))
        self._last = found
        return found

    def evidence(self, st) -> bool:
        return False

    def on_nudge(self, st) -> None:
        # The next check looks only at what the model runs after this nudge;
        # what was found is kept for an answer that runs nothing new.
        st.rule_check_mark = len(_commands(st.session_id))
        st.rule_breaks_pending = self._last or []

    def notice(self, claims) -> str:
        return "⚠ a command broke one of your rules — sending the agent back"

    def nudge(self, claims) -> str:
        lines = "\n".join(f"- `{c[:200]}` breaks: {r}" for c, r in claims)
        return ("[rule check — not the user] These commands of this turn did what "
                f"the user's rules forbid:\n{lines}\nWhat they produced does not "
                "count. Do the work the way the rules say. If a rule cannot be "
                "followed (the host is unreachable, a tool is missing), stop and "
                "tell the user exactly what blocks it, and do NOT report the task "
                "as done.")

    def disclaimer(self, claims) -> str:
        lines = "\n".join(f"- `{c[:160]}` — your rule: {r}" for c, r in claims)
        return ("⚠ **Harness rule check:** this turn ran commands your rules "
                f"forbid:\n{lines}\nTreat what they produced with care.\n\n")


__all__ = ["RuleBreakGuard", "breaks", "enabled"]
