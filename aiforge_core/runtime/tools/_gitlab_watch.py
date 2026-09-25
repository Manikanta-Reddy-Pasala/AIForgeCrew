"""Watching a GitLab pipeline until it finishes: the budget, the envelopes, and
the gitlab_pipeline_watch tool."""
from __future__ import annotations

from ._gitlab_pipes import (
    _is_fatal,
    _pipe_env,
    _pipe_int,
)


def _pkg():
    """``gitlab``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``gitlab``; patch any other
    name on this module."""
    import aiforge_core.runtime.tools.gitlab as package
    return package


class _WatchBudget:
    """How long one pipeline watch may run, and whether Stop can reach it."""

    __slots__ = ("interval", "budget", "max_checks", "sid", "chat_cancel",
                 "unattended")

    def __init__(self, args: dict) -> None:
        self.interval = _pipe_int(args, "interval_s", 20, 5, 3600)
        # This SLEEPS inside one tool call on the producer thread: the step cap,
        # the turn deadline and mid-run steering are all checked BETWEEN steps,
        # so none of them bound it, and there are only eight producer slots.
        # Same discipline and the same ceiling as watch_until.
        self.budget = _pipe_int(args, "timeout_s", 600, 10,
                                _pipe_env("AIFORGE_GITLAB_WATCH_MAX_SECONDS", 1800))
        self.max_checks = _pipe_int(args, "max_checks", 60, 1,
                                    _pipe_env("AIFORGE_GITLAB_WATCH_MAX_CHECKS", 200))
        self.chat_cancel = None
        self.sid = None
        try:
            from aiforge_core.runtime import chat_cancel as _cc
            self.chat_cancel = _cc
            self.sid = _cc.active()
        except Exception:  # noqa: BLE001 — no cancel machinery → unattended
            self.sid = None
        self.unattended = self.sid is None
        if self.unattended:
            # No cancel handle at all: the jobs runner and /api/chat/agent pass
            # session_id=None, and chat_cancel is a ContextVar that does not
            # cross into a worker thread. NOTHING can interrupt this loop, so it
            # does not get a long one — fail SHORT, not open. Reported back,
            # because a caller that asked for 600s and silently got 180 cannot
            # explain its own timeout.
            #
            # NOT the team/ADK path: chat_pipeline._drive and
            # parallel_subtasks._stream both call `chat_cancel.set_active(...)`
            # inside their driver thread precisely so Stop reaches the tools they
            # run. Those get a real sid, so they are attended and keep the full
            # budget — which is why the doer wrapper's docstring had to stop
            # promising a clamp.
            self.budget = min(self.budget, _pipe_env(
                "AIFORGE_GITLAB_WATCH_UNATTENDED_SECONDS", 180))
            self.max_checks = min(self.max_checks, 10)

    def cancelled(self) -> bool:
        return not self.unattended and self.chat_cancel.is_cancelled(self.sid)


def _watch_envelope(checks: int, started: float) -> dict:
    # No `requests` key. It counted gitlab_pipeline CALLS, so it was always
    # `checks` or `checks + 1` — carrying no information beyond `checks` while
    # its name implied HTTP volume, which is 1-20x higher.
    import time as _time
    return {"checks": checks,
            "elapsed_s": round(_time.monotonic() - started, 1)}


def _watch_stopped(state: dict, checks: int, started: float, err: dict) -> dict:
    # SPREAD FIRST, explicit keys last. The other order let `last["ok"]`
    # overwrite `ok: False`, so a watch the user stopped came back as a watch
    # that succeeded — and an error dict overwrote "stopped by user" with an
    # HTTP error, making Stop indistinguishable from a failure.
    out = {**state, **_watch_envelope(checks, started),
           "ok": False, "stopped": True, "error": "stopped by user"}
    if err and state:
        # The snapshot is real but old — the same thing the timeout path says,
        # and for the same reason.
        out["stale"] = True
        out["last_poll_error"] = err.get("error")
    return out


def _watch_finished(args: dict, cwd, res: dict, pinned, checks: int,
                    started: float) -> dict:
    """The pipeline is done — now, and only now, pay for the jobs and logs."""
    final = _pkg().gitlab_pipeline({**args, "pipeline_id": pinned}, cwd)
    if final.get("ok"):
        return {**final, **_watch_envelope(checks, started)}
    # The ONE call that was going to fetch the logs failed (a 429 right at
    # completion is common). Falling back silently to the logs=False snapshot
    # hands back a clean, finished, failed result naming a job with no log and
    # no reason there is no log — which reads as "there was no log".
    return {**res, **_watch_envelope(checks, started),
            "logs_error": final.get("error"),
            "hint": ("the pipeline finished, but re-reading it for the job logs "
                     "failed — read it again with gitlab_pipeline")}


def _watch_fatal(good: dict, res: dict, checks: int, started: float) -> dict:
    """A bad token or a missing project fails identically forever; looping on it
    burns the whole budget to learn nothing.

    But we may already have READ this pipeline (a token rotated mid-watch, a
    project archived). The docstring promises `passed` on every return that
    observed the pipeline at all — discarding `good` here broke that promise on
    the one path that had the data and dropped it.
    """
    out = {**good, **res, **_watch_envelope(checks, started)}
    if good:
        out["stale"] = True
        out["last_poll_error"] = res.get("error")
    return out


def _watch_sleep(b: "_WatchBudget") -> "str | None":
    """Sleep one interval in 1s slices. ``"stop"`` / ``"steer"`` when the user
    pressed Stop or typed a message during the wait, else None.

    One second, not a fifth: the budget math and the tests count whole seconds
    of ``time.sleep``, and a message is still seen long before a 20s poll."""
    from aiforge_core.runtime.run_interrupt import pause
    return pause(b.interval, b.sid, slice_s=1.0)


def _watch_timeout(b: "_WatchBudget", good: dict, err: dict, checks: int,
                   started: float) -> dict:
    tail = {**_watch_envelope(checks, started), "timed_out": True}
    if b.unattended:
        # Both halves, and only the one that BIT. Reporting the seconds budget
        # unconditionally read as "the 180s ran out" when what actually ended the
        # run was the 10-check cap at 45s — the same complaint about a silently
        # shortened budget, one field over.
        if checks >= b.max_checks:
            tail["unattended_max_checks"] = b.max_checks
        else:
            tail["unattended_budget_s"] = b.budget
    if not good:
        # We never once read the pipeline. Saying ok:True here handed the agent a
        # successful-looking envelope carrying an HTTP error and no `passed` key
        # at all — the one shape from which a model can tell a user the build
        # passed when nothing was ever observed.
        return {**(err or {"ok": False, "error": "no_successful_poll"}),
                **tail, "ok": False,
                "reason": "the pipeline was never successfully read"}
    out = {**good, **tail, "ok": True}
    if err:
        # Ended on a failed poll: the data is the last GOOD one, and it is old.
        out["stale"] = True
        out["last_poll_error"] = err.get("error")
    out["reason"] = (f"still {good.get('status') or 'unknown'} after "
                     f"{checks} check(s) — the watch gave up, "
                     f"the pipeline did not")
    return out


def _poll_args(args: dict, pinned) -> dict:
    """Args for ONE poll.

    ``logs=False`` while polling: a stage-1 failure with later stages still
    running re-downloaded up to three job traces on EVERY poll and threw all but
    the last away — fetched once, on the check that finishes. ``skip_jobs`` at
    the call site does the same for the job list: only ``finished`` decides
    whether to keep polling, and walking up to five 100-job pages every poll
    re-fetched — and discarded — six times the HTTP the watch actually needed.
    """
    out = {**args, "logs": False}
    if pinned:
        out["pipeline_id"] = pinned
    return out


def _one_check(args: dict, cwd, pinned, good: dict, _err: dict, checks: int,
               started: float):
    """One poll. Returns ``(final_result_or_None, good, err, pinned)``."""
    res = _pkg().gitlab_pipeline(_poll_args(args, pinned), cwd, skip_jobs=True)
    if not res.get("ok"):
        if _is_fatal(res):
            return _watch_fatal(good, res, checks, started), good, res, pinned
        return None, good, res, pinned
    # PIN the id after the first successful resolve. A ref-addressed watch
    # re-ran "latest pipeline on this ref" every poll, so a colleague pushing
    # mid-watch silently re-targeted it — and it would then report `passed` for
    # a pipeline the user never asked about while theirs failed.
    pinned = pinned or res.get("id")
    if res.get("finished"):
        return (_watch_finished(args, cwd, res, pinned, checks, started),
                res, {}, pinned)
    return None, res, {}, pinned


def gitlab_pipeline_watch(args: dict, cwd: str | None = None) -> dict:
    """WATCH one CI pipeline until it finishes (or the budget runs out).

    ONE tool call covers the whole watch — no model request per poll, which is
    the entire point now that model calls are rate-ceilinged. Same addressing
    as :func:`gitlab_pipeline`. Optional ``interval_s`` (default 20),
    ``timeout_s`` (default 600), ``max_checks``.

    Returns ``checks`` and ``elapsed_s`` always. A run that ENDS on a finished
    pipeline returns the full ``gitlab_pipeline`` shape (jobs, failed_jobs,
    logs); a run that is stopped, gives up, or hits a fatal error returns the
    last snapshot it read — which was polled without jobs or logs, so those
    keys are absent. ``ok`` says the WATCH worked; whether the pipeline passed
    is ``passed``, present on every return that observed the pipeline at all.
    """
    pkg = _pkg()
    import time as _time
    if not pkg._proj_id(args):
        return {"ok": False, "error": pkg._MISSING_PROJECT,
                "hint": pkg._PROJECT_HINT}
    b = _WatchBudget(args)
    started = _time.monotonic()
    checks = 0
    pinned = args.get("pipeline_id") or args.get("id") or None
    good: dict = {}          # the last snapshot we actually READ
    err: dict = {}           # the last failed poll, if the run ended on one
    from aiforge_core.runtime.run_interrupt import reason, steered
    while checks < b.max_checks:
        # One probe. Calling cancelled() and then reason() asked is_cancelled
        # twice, so a Stop that was meant to land in the sleep fired before
        # the first poll and dropped the snapshot.
        why = reason(b.sid)
        if why == "stop":
            return _watch_stopped(good, checks, started, err)
        if why == "steer":
            return {**(good or {}), **_watch_envelope(checks, started),
                    **steered()}
        checks += 1
        done, good, err, pinned = _one_check(args, cwd, pinned, good, err,
                                             checks, started)
        if done is not None:
            return done
        if (_time.monotonic() - started) + b.interval > b.budget \
                or checks >= b.max_checks:
            break
        why = _watch_sleep(b)
        if why == "stop":
            return _watch_stopped(good, checks, started, err)
        if why == "steer":
            return {**(good or {}), **_watch_envelope(checks, started),
                    **steered()}
    # Budget / max_checks break skips the sleep. Look once more so a message
    # typed during the last poll is not reported as a successful give-up.
    why = reason(b.sid)
    if why == "stop":
        return _watch_stopped(good, checks, started, err)
    if why == "steer":
        return {**(good or {}), **_watch_envelope(checks, started),
                **steered()}
    return _watch_timeout(b, good, err, checks, started)
