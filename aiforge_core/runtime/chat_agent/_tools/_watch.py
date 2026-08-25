"""Two capabilities the chat agent had no way to express: KEEP CHECKING until
something is true, and DO THIS LATER, repeatedly.

Before this, "monitor the deploy until it's healthy" could only be served by
the agent re-issuing `run_command` in its own ReAct loop — one model call per
check, which burns the step budget, the request-rate ceiling and the user's
patience, and stops the moment the turn ends. "Run this every morning" could
not be served at all; the jobs scheduler existed but only the Jobs UI could
reach it.

Both are TOOLS, deliberately. Nothing here sniffs the user's words for
"monitor" or "every day" — the model decides from the tool descriptions, the
same way it decides to read a file. A keyword list would be wrong in both
directions: it would miss "let me know when the queue drains" and fire on
"the monitor is broken".
"""
from __future__ import annotations

import os
import re
import time

_STOPPED_BY_USER = 'stopped by user'

# Two tails at 4000 overshot the loop's own 6000-char observation cap, so the
# second one was silently re-truncated downstream. Half each is honest.
_MAX_TAIL = 2000

# Output handed to a regex. A model-supplied pattern over unbounded command
# output is a ReDoS in the producer thread — and `re.search` holds the
# interpreter, so neither Stop nor a signal can preempt it: the turn hangs
# forever, the session stays "running" (every later message 409s) and one of
# the eight global producer slots is leaked for the life of the process.
_MAX_MATCH_CHARS = 8000

# Patterns whose backtracking is exponential. Refused up front rather than
# discovered at runtime, because at runtime there is no way back.
_REDOS_RE = re.compile(r"\([^)]*[+*]\)[+*]|\((?:[^)|]+\|)+[^)|]+\)[+*]")


def _int(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(args.get(key, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


_CONDITIONS = ("exit_zero", "exit_nonzero", "contains:TEXT",
               "not_contains:TEXT", "regex:PATTERN")


def _compile_condition(until: str) -> "tuple[object | None, str | None]":
    """Validate the condition ONCE, before the first check.

    A bad regex used to fail identically on every check, so an unparseable
    pattern cost the whole budget to discover — the exact waste the refused-
    command fast path exists to avoid. An unrecognised spelling used to fall
    back to exit_zero, which is worse than useless: `until="contains Running"`
    (a missing colon) reported matched=True the instant the command exited 0,
    and the agent told the user the pods were Running.
    """
    cond = (until or "exit_zero").strip()
    low = cond.lower()
    if low in ("exit_zero", "ok", "success", "exit_nonzero", "fail",
               "failure", "error"):
        return None, None
    for prefix in ("contains:", "not_contains:", "regex:"):
        if low.startswith(prefix):
            return _compile_prefixed(prefix, cond[len(prefix):].strip())
    return None, (f"unrecognised condition {cond!r} — use one of: "
                  + ", ".join(_CONDITIONS))


def _compile_prefixed(prefix: str, needle: str) -> "tuple[object | None, str | None]":
    """Validate one ``contains:/not_contains:/regex:`` condition, compiling the
    regex (with a ReDoS guard) only for ``regex:``."""
    if not needle:
        return None, f"`{prefix}` needs something to look for."
    if prefix != "regex:":
        return None, None
    if _REDOS_RE.search(needle):
        return None, (f"regex {needle!r} has nested quantifiers, which "
                      "can hang for exponential time on ordinary "
                      "output. Use a simpler pattern or contains:.")
    try:
        return re.compile(needle, re.I | re.M), None
    except re.error as exc:
        return None, f"bad regex {needle!r}: {exc}"


def _matches(until: str, res: dict, rx=None) -> "tuple[bool, str]":
    """Did this check satisfy the stop condition? Returns (matched, why)."""
    # Bounded before matching: see _MAX_MATCH_CHARS.
    out = (f"{res.get('stdout') or ''}\n{res.get('stderr') or ''}")[-_MAX_MATCH_CHARS:]
    cond = (until or "exit_zero").strip()
    low = cond.lower()
    if low in ("exit_zero", "ok", "success"):
        return bool(res.get("ok")), "command exited 0"
    if low in ("exit_nonzero", "fail", "failure", "error"):
        # A command that TIMED OUT or never spawned also reports ok=False, and
        # "the service went down" is not the same fact as "our probe was slow".
        if res.get("timed_out") or res.get("code") is None:
            return False, "the check itself did not complete"
        return not res.get("ok"), "command exited non-zero"
    needle = cond.split(":", 1)[1].strip() if ":" in cond else ""
    # Case-insensitive, like the regex branch: `contains:running` against
    # `Running` silently burned entire budgets.
    if low.startswith("contains:"):
        return (needle.lower() in out.lower()), f"output contains {needle!r}"
    if low.startswith("not_contains:"):
        return (needle.lower() not in out.lower()), \
            f"output no longer contains {needle!r}"
    if rx is not None:
        return bool(rx.search(out)), f"output matches /{needle}/"
    # _compile_condition rejects anything else before the loop starts.
    return bool(res.get("ok")), "command exited 0"


def _watch_limits(args: dict, sid) -> tuple[int, int, int, int]:
    """Resolve (interval_s, max_checks, budget_s, per_cmd_s) for a watch,
    tightening them when there is no cancel handle.

    The watch sleeps INSIDE a tool call on the producer thread: the step cap,
    the turn deadline and mid-run steering are all checked between steps, so
    none of them bound it. Defaults 5min / hard ceiling 30 — the old 6h ceiling
    meant one call could hold a producer slot (there are 8) for a working day.
    With no cancel handle (unattended callers pass session_id=None; chat_cancel
    is a ContextVar that does not cross into a worker thread) Stop cannot reach
    the watch, so it fails SHORT rather than open."""
    interval = _int(args, "interval_s", 30, 1, 3600)
    max_checks = _int(args, "max_checks", 20, 1,
                      _env_int("AIFORGE_WATCH_MAX_CHECKS", 60))
    budget = _int(args, "timeout_s", 300, 5,
                  _env_int("AIFORGE_WATCH_MAX_SECONDS", 1800))
    per_cmd = _int(args, "cmd_timeout", 120, 1, 3600)
    if sid is None:
        budget = min(budget, _env_int("AIFORGE_WATCH_UNATTENDED_SECONDS", 120))
        max_checks = min(max_checks, 5)
    # A per-command timeout longer than the whole budget lets one check run past
    # the ceiling the caller asked for.
    return interval, max_checks, budget, min(per_cmd, budget)


def _watch_sleep(interval: float, sid) -> bool:
    """Sleep ``interval`` in 1s slices so Stop is honoured mid-wait. Returns True
    if the watch was cancelled during the sleep."""
    from aiforge_core.runtime import chat_cancel
    waited = 0.0
    while waited < interval:
        if sid is not None and chat_cancel.is_cancelled(sid):
            return True
        time.sleep(min(1.0, interval - waited))
        waited += 1.0
    return False


def _check_outcome(last: dict, until: str, rx, checks: int,
                   elapsed: float) -> "dict | None":
    """The terminal result of one watch check (stopped / blocked / matched), or
    None to keep watching. Reuses run_command's refusal + cancellation guards —
    a second "run a shell command" would be a second place to forget them."""
    if last.get("stopped"):
        return {"ok": False, "stopped": True, "checks": checks,
                "error": _STOPPED_BY_USER, "last": _tail(last)}
    if last.get("blocked"):
        # A refused command will be refused every time — looping is waste.
        return {"ok": False, "checks": checks, "blocked": last["blocked"],
                "error": last.get("error"), "last": _tail(last)}
    matched, why = _matches(until, last, rx)
    if matched:
        return {"ok": True, "matched": True, "checks": checks,
                "elapsed_s": elapsed, "reason": why, "last": _tail(last)}
    return None


def _t_watch_until(args: dict, cwd: str) -> dict:
    """Re-run one command until a condition holds, or the budget runs out.

    ONE tool call covers the whole watch: no model call per check. That is the
    point — a 30-check watch costs one request, not thirty, which matters now
    that there is a per-minute ceiling on model calls.
    """
    from .._shell import _t_run_command
    from aiforge_core.runtime import chat_cancel
    cmd = (args.get("cmd") or "").strip()
    if not cmd:
        return {"ok": False, "error": "watch_until needs a `cmd` to run."}
    until = str(args.get("until") or "exit_zero")
    rx, cond_err = _compile_condition(until)
    if cond_err:
        # Fail NOW, not after twenty checks discover the same thing.
        return {"ok": False, "error": cond_err}
    sid = chat_cancel.active()
    interval, max_checks, budget, per_cmd = _watch_limits(args, sid)
    started = time.monotonic()
    checks = 0
    last: dict = {}
    while checks < max_checks:
        if sid is not None and chat_cancel.is_cancelled(sid):
            return {"ok": False, "stopped": True, "checks": checks,
                    "error": _STOPPED_BY_USER}
        checks += 1
        last = _t_run_command({"cmd": cmd, "timeout": per_cmd}, cwd)
        done = _check_outcome(last, until, rx, checks,
                              round(time.monotonic() - started, 1))
        if done is not None:
            return done
        if round(time.monotonic() - started, 1) + interval > budget \
                or checks >= max_checks:
            break
        if _watch_sleep(interval, sid):
            return {"ok": False, "stopped": True, "checks": checks,
                    "error": _STOPPED_BY_USER, "last": _tail(last)}
    return {"ok": False, "matched": False, "checks": checks,
            "elapsed_s": round(time.monotonic() - started, 1),
            "reason": f"gave up after {checks} check(s) — the condition "
                      f"({until}) was never met",
            "last": _tail(last)}


def _tail(res: dict) -> dict:
    return {"code": res.get("code"),
            "stdout": (res.get("stdout") or "")[-_MAX_TAIL:],
            "stderr": (res.get("stderr") or "")[-_MAX_TAIL:]}


def _cron_from(args: dict) -> "tuple[str | None, str | None]":
    """(cron, error) from either an explicit cron or `every_minutes`."""
    cron = (args.get("cron") or "").strip()
    if cron:
        return cron, None
    every = args.get("every_minutes")
    if every in (None, ""):
        return None, ("schedule_task needs either `cron` (5-field crontab) or "
                      "`every_minutes`.")
    try:
        n = int(every)
    except (TypeError, ValueError):
        return None, f"every_minutes must be a whole number, got {every!r}"
    if n <= 0:
        return None, "every_minutes must be 1 or more."
    if n < 60 and 60 % n == 0:
        return f"*/{n} * * * *", None
    if n == 60:
        return "0 * * * *", None
    if n % 60 == 0 and n <= 24 * 60 and 24 % (n // 60) == 0:
        return f"0 */{n // 60} * * *", None
    return None, (f"every_minutes={n} does not map to a clean crontab slot. "
                  "Use a divisor of 60 (5, 10, 15, 20, 30), a whole number of "
                  "hours that divides 24, or pass `cron` directly.")


def _interval_minutes(cron: str) -> "int | None":
    """Minutes between fires for the simple `*/N * * * *` / `0 */H * * *`
    shapes, else None (an arbitrary crontab is the operator's business)."""
    parts = (cron or "").split()
    if len(parts) != 5:
        return None
    minute, hour, dom, mon, dow = parts
    if (hour, dom, mon, dow) == ("*", "*", "*", "*"):
        if minute == "*":
            return 1
        if minute.startswith("*/") and minute[2:].isdigit():
            return max(1, int(minute[2:]))
    if minute.isdigit() and hour.startswith("*/") and hour[2:].isdigit() \
            and (dom, mon, dow) == ("*", "*", "*"):
        return max(1, int(hour[2:])) * 60
    return None


def _schedule_list(jobs_store) -> dict:
    """List scheduled jobs. `last_error` is deliberately NOT returned: for a
    script job it is a 500-char stderr tail from an arbitrary local ops script —
    paths, hostnames, whatever it printed — and forwarding that to the model
    ships it to the provider. A boolean answers "is it failing?" without the
    payload."""
    return {"ok": True, "jobs": [
        {"id": j.get("id"), "name": j.get("name"), "cron": j.get("cron"),
         "kind": j.get("kind"), "next_run_at": j.get("next_run_at"),
         "enabled": j.get("enabled"),
         "failing": bool(j.get("last_error"))} for j in jobs_store.list_jobs()]}


def _schedule_cancel(args: dict, jobs_store) -> dict:
    """Cancel a ticket job by numeric id. A script job is a host shell script
    the OPERATOR installed — this tool schedules instructions, so it does not
    get to delete those."""
    try:
        job_id = int(args.get("job_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "cancel needs a numeric `job_id` "
                                      "(use action=list to find it)."}
    existing = jobs_store.get(job_id)
    if not existing:
        return {"ok": False, "error": f"no job {job_id}"}
    if (existing.get("kind") or "ticket") != "ticket":
        return {"ok": False, "error":
                f"job {job_id} is a {existing.get('kind')} job installed "
                "outside chat — cancel it from the Jobs page."}
    return ({"ok": True, "cancelled": job_id} if jobs_store.delete(job_id)
            else {"ok": False, "error": f"no job {job_id}"})


def _schedule_validate_cron(cron: str, jobs_parse) -> "dict | None":
    """Cron guard: reject a too-frequent interval (each fire files a ticket the
    pipeline then builds) or an unschedulable expression. None when OK."""
    floor = _env_int("AIFORGE_SCHEDULE_MIN_MINUTES", 15)
    every = _interval_minutes(cron)
    if every is not None and every < floor:
        return {"ok": False, "error":
                f"`{cron}` fires every {every} minute(s); the floor is "
                f"{floor} because each run files a ticket that the pipeline "
                "works autonomously. Use a longer interval, or raise "
                "AIFORGE_SCHEDULE_MIN_MINUTES."}
    if not jobs_parse.schedulable(cron):
        # Covers both "croniter missing" and "valid syntax, impossible date".
        return {"ok": False, "error":
                f"`{cron}` is not a schedulable crontab expression "
                "(or the croniter package is not installed)."}
    return None


def _schedule_create(args: dict, jobs_parse, jobs_store) -> dict:
    """Create a recurring ticket job from an instruction + cron."""
    instruction = (args.get("instruction") or args.get("prompt") or "").strip()
    if not instruction:
        return {"ok": False, "error": "schedule_task needs an `instruction` — "
                                      "what should happen on each run."}
    # Bounded like the natural-language job path (jobs/parse.py) rather than
    # storing whatever arrives: a runaway instruction becomes every ticket body
    # this job ever files.
    instruction = instruction[:4000]
    name = ((args.get("name") or "").strip() or instruction[:60])[:120]
    cron, err = _cron_from(args)
    if err:
        return {"ok": False, "error": err}
    bad = _schedule_validate_cron(cron, jobs_parse)
    if bad is not None:
        return bad
    # A retry after a transient failure must not double-schedule: two rows with
    # the same name both fire every slot, so every run files two tickets.
    for j in jobs_store.list_jobs():
        if (j.get("name") or "").strip().lower() == name.strip().lower():
            return {"ok": False, "error":
                    f"a job named {name!r} already exists (id {j.get('id')}, "
                    f"cron {j.get('cron')}). Cancel it first, or use another "
                    "name.", "job_id": j.get("id")}
    try:
        nxt = jobs_parse.next_runs(cron, n=1)[0]
        job = jobs_store.create(
            name=name, cron=cron, ticket_title=name[:120],
            ticket_body=instruction, project=args.get("project") or None,
            next_run_at=nxt, kind="ticket")
    except Exception as exc:  # noqa: BLE001 — surface, never crash the turn
        return {"ok": False, "error": f"could not schedule: {exc}"}
    return {"ok": True, "job_id": job.get("id"), "name": job.get("name"),
            "cron": cron, "next_run_at": job.get("next_run_at"),
            "note": "Each run files a ticket with this instruction."}


def _t_schedule_task(args: dict, _cwd: str) -> dict:
    """Create / list / cancel a recurring task in the jobs scheduler.

    The scheduler already existed; it just had no door from chat. Each run
    files a ticket carrying the instruction, which the pipeline then works —
    so a scheduled task is a real unit of work with a trail, not a cron line
    hidden inside a chat session that ends.
    """
    from aiforge_core.jobs import parse as jobs_parse
    from aiforge_core.jobs import store as jobs_store

    action = (args.get("action") or "create").strip().lower()
    if action in ("list", "ls"):
        return _schedule_list(jobs_store)
    if action in ("cancel", "delete", "remove", "stop"):
        return _schedule_cancel(args, jobs_store)
    if action != "create":
        return {"ok": False, "error": f"unknown action {action!r} — use "
                                      "create, list or cancel."}
    return _schedule_create(args, jobs_parse, jobs_store)


__all__ = ["_t_watch_until", "_t_schedule_task"]
