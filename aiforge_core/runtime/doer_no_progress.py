"""The no-progress rule (:mod:`aiforge_core.runtime.no_progress`) for the
native pipeline Doer, which runs its tools through ADK callbacks instead of
the chat loop.

Progress here: a tool call other than a shell command with arguments not
seen before (a new file read, a new edit, a new lookup), a shell read of a
new path, fewer failing tests or a suite turning green, or a running
command still producing output. A first trip rides a note on the tool
response; a second one flags the Doer loop to exit partial with the reason
``no_progress`` (no replan) and tells the model to finish now.
"""
from __future__ import annotations

import collections
import threading

from aiforge_core.runtime import no_progress
from aiforge_core.runtime.cmd_finished import as_run
from aiforge_core.runtime.failure_signature import failure_of, result_text

_CHECK_INS = ("command_wait", "command_output")
_MAX_RUNS = 64
_MAX_SEEN = 20_000


class DoerProgressGuard:
    """One per callback; its trackers are keyed by run (invocation)."""

    def __init__(self) -> None:
        self._runs: collections.OrderedDict = collections.OrderedDict()
        # ADK may run a turn's tool calls in parallel: one step at a time.
        self._lock = threading.RLock()

    def _run(self, key) -> dict:
        run = self._runs.pop(key, None) or {"track": {}, "seen": collections.OrderedDict(),
                                           "fails": None, "stopped": ""}
        self._runs[key] = run
        while len(self._runs) > _MAX_RUNS:
            self._runs.popitem(last=False)
        return run

    def _new(self, run, key: str) -> bool:
        if key in run["seen"]:
            return False
        run["seen"][key] = True
        while len(run["seen"]) > _MAX_SEEN:
            run["seen"].popitem(last=False)
        return True

    @staticmethod
    def _fails_moved(run, name, args, res) -> bool:
        """Fewer failing tests or red turned green in a FINISHED run (a
        checked-on command that ended counts as its original command; one
        still running is neither a pass nor a failure)."""
        done = as_run(name, args, res)
        if done is None or (done[0] != "run_tests"
                            and done[0] not in no_progress._SHELL_TOOLS):
            return False
        res = done[2]
        if res.get("stopped") or res.get("timed_out"):
            return False
        fail = failure_of(result_text(res)) if res.get("ok") is False else None
        if fail is not None and fail.signature:
            moved = run["fails"] is not None and fail.count < run["fails"]
            run["fails"] = fail.count
            return moved
        if res.get("ok") is True and run["fails"]:
            run["fails"] = 0
            return True
        return False

    def _progress(self, run, name, args, res) -> bool:
        progressed = self._fails_moved(run, name, args, res)
        if name in _CHECK_INS:
            return bool(res.get("output_growing") or res.get("cpu_active")
                        or res.get("running") is False) or progressed
        if name in no_progress._SHELL_TOOLS:
            paths = no_progress.shell_read_paths(args)
            return any([self._new(run, f"path:{p}") for p in paths]) or progressed
        return self._new(run, no_progress.command_template(name, args)) or progressed

    def step(self, run_key, name, args, response, state):
        """Feed one Doer tool call. Returns None (leave the response alone)
        or a replacement response carrying the loop guard's note."""
        if not isinstance(response, dict):
            return None
        with self._lock:
            return self._step(run_key, name, args, response, state)

    def _step(self, run_key, name, args, response, state):
        run = self._run(run_key)
        if run["stopped"]:
            return {**response, "loop_guard": run["stopped"]}
        progress = self._progress(run, name, args if isinstance(args, dict) else {},
                                  response)
        verdict = no_progress.observe(
            run["track"], no_progress.command_template(name, args),
            no_progress.output_class(name, result_text(response)), progress)
        reason = str(run["track"].get("last") or "")
        if verdict == no_progress.NUDGE:
            return {**response, "loop_guard": no_progress.nudge_text(reason)}
        if verdict == no_progress.STOP:
            run["stopped"] = ("[loop guard — not the user] Still no progress after "
                              "the warning. Stop calling tools and finish NOW "
                              "with what you have and why the goal is not "
                              "converging.")
            if state is not None:
                state["loop_budget_kill"] = True
                state["loop_budget_reason"] = "no_progress"
            return {**response, "loop_guard": run["stopped"]}
        return None


__all__ = ["DoerProgressGuard"]
