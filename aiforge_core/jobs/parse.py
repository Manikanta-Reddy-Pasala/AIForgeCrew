"""NL instructions → job draft. ONE capped triage-tier LLM call at
creation time (same conventions as rule_capture.classify: strict JSON,
temperature 0, brace-balanced extraction), then deterministic croniter
validation. Fails CLOSED — unlike runtime paths, a parse/validation
error here blocks the save; a bad job must never be born. The LLM never
runs per-tick; scheduling is pure cron math."""
from __future__ import annotations

import logging
import os
from datetime import datetime

from croniter import croniter

# Reuse the battle-tested brace-balanced JSON extractor (string-aware);
# duplicating 30 lines of parser is worse than this private import.
from aiforge_core.runtime.rule_capture import _extract_json

log = logging.getLogger("aiforge.jobs")

_REQUIRED = ("name", "cron", "ticket_title", "ticket_body")

_SYS = (
    "You turn a user's natural-language request for a RECURRING job into "
    "strict JSON. The job fires on a cron schedule and each fire creates "
    "a ticket for an autonomous coding agent to execute.\n\n"
    "Rules:\n"
    "- \"cron\": a standard 5-field cron expression for the schedule the "
    "user described. Times are the server's local time.\n"
    "- \"name\": a short human label (3-6 words).\n"
    "- \"ticket_title\": a one-line imperative title for the ticket.\n"
    "- \"ticket_body\": clear, self-contained instructions the agent can "
    "act on without this conversation's context.\n"
    "- \"project\": the target repo/project name if the user named one, "
    "else null.\n\n"
    "Respond with STRICT JSON ONLY, no prose, no code fence:\n"
    '{"name":"...","cron":"m h dom mon dow","ticket_title":"...",'
    '"ticket_body":"...","project":null}'
)

_DAYS = {"0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
         "4": "Thursday", "5": "Friday", "6": "Saturday", "7": "Sunday"}


def _timeout_s() -> int:
    try:
        return max(1, int(os.environ.get("AIFORGE_JOBS_PARSE_TIMEOUT_S", "60")))
    except (TypeError, ValueError):
        return 60


def human_schedule(cron: str) -> str:
    """Plain-words description for the common shapes; raw cron fallback."""
    parts = (cron or "").split()
    if len(parts) != 5:
        return f"cron: {cron}"
    m, h, dom, mon, dow = parts
    if m.startswith("*/") and h == "*" and (dom, mon, dow) == ("*", "*", "*"):
        return f"Every {m[2:]} minutes"
    if m.isdigit() and h.isdigit() and (dom, mon) == ("*", "*"):
        hhmm = f"{int(h):02d}:{int(m):02d}"
        if dow == "*":
            return f"Every day at {hhmm}"
        if dow == "1-5":
            return f"Weekdays at {hhmm}"
        if dow in _DAYS:
            return f"Every {_DAYS[dow]} at {hhmm}"
    return f"cron: {cron}"


def next_runs(cron: str, n: int = 3, base: datetime | None = None) -> list[str]:
    it = croniter(cron, base or datetime.now())
    return [it.get_next(datetime).isoformat(timespec="seconds")
            for _ in range(n)]


def schedulable(cron: str) -> bool:
    """True only if ``cron`` both validates AND can actually produce a next
    run. croniter.is_valid() accepts impossible date/month combos (e.g.
    "0 0 31 2 *" — Feb 31) that then raise on get_next(); this catches
    that so callers never crash on a save-valid-but-unschedulable cron."""
    if not croniter.is_valid(cron):
        return False
    try:
        croniter(cron, datetime.now()).get_next(datetime)
        return True
    except Exception:  # noqa: BLE001 — CroniterBadDateError et al.
        return False


def parse_instructions(instructions: str) -> dict:
    """→ {"ok": True, "draft": {...}, "human_schedule": str,
    "next_runs": [iso, iso, iso]} or {"ok": False, "error": str}."""
    text = (instructions or "").strip()
    if not text:
        return {"ok": False, "error": "empty instructions"}
    try:
        from aiforge_core.llm import client
        raw = client.complete("triage", [
            {"role": "system", "content": _SYS},
            {"role": "user", "content": text[:4000]},
        ], temperature=0.0, max_tokens=600, timeout_s=_timeout_s())
    except Exception as exc:  # noqa: BLE001 — fail closed with a message
        log.warning("jobs.parse llm error: %s", exc)
        return {"ok": False, "error": "parser unavailable — check the "
                                      "triage model and try again"}
    obj = _extract_json(raw or "")
    if not isinstance(obj, dict):
        return {"ok": False, "error": "could not parse the instructions — "
                                      "try rephrasing (e.g. 'every day at 8am, "
                                      "pull the GitLab comments')"}
    missing = [k for k in _REQUIRED if not str(obj.get(k) or "").strip()]
    if missing:
        return {"ok": False,
                "error": f"parse incomplete — missing {', '.join(missing)}"}
    if any(str(obj.get(k) or "").strip() == "..." for k in _REQUIRED):
        return {"ok": False, "error": "parser returned a template "
                                      "placeholder — try rephrasing"}
    cron = str(obj["cron"]).strip()
    if not schedulable(cron):
        return {"ok": False,
                "error": f"invalid or unschedulable cron from parse: {cron!r}"}
    project = obj.get("project")
    draft = {
        "name": str(obj["name"]).strip()[:120],
        "cron": cron,
        "ticket_title": str(obj["ticket_title"]).strip()[:200],
        "ticket_body": str(obj["ticket_body"]).strip()[:4000],
        "project": (str(project).strip() or None) if project else None,
    }
    return {"ok": True, "draft": draft,
            "human_schedule": human_schedule(cron),
            "next_runs": next_runs(cron)}
