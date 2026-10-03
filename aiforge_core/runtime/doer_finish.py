"""``finish`` ends the Doer's turn, and the Doer's outcome is what it did.

The Doer's ``finish`` tool returns ``terminate: True``, which the old
``LoopAgent`` acted on. In the ``Workflow`` graph nothing did: the turn ended
only when the model happened to answer with text and no tool call. A model
that answered its own ``finish`` with one more check and another ``finish``
kept going — live, 162 model calls and 15 minutes (stopped by hand) on a
two-line change that was written, tested and committed in the first 46 seconds.

:func:`make_finish_callback` (after-tool) makes the signal real: a ``finish``
that succeeded ends the agent's turn (ADK ``skip_summarization``: the tool
response is the final event, the model is not called again).

The Doer's outcome is the ``{file_diffs, compile_status, test_status,
turn_log}`` contract the Validator reads. It used to be whatever the model
wrote last — often prose with no ``file_diffs``, which the Validator answers
with ``request_changes`` and a re-plan (minutes) for work that was done.
:func:`make_outcome_callback` (after-agent) writes the contract from what the
turn DID: the files its write tools changed and the result of its last test
run, with the model's closing words as the log line. A field nothing measured
is left as the model gave it, or left out — never guessed.

``AIFORGE_DOER_FINISH_ENDS_TURN=0`` restores the earlier behaviour of both.
"""
from __future__ import annotations

import json
import os

#: The state key the Doer's closing text is saved under (``agents.doer``).
OUTCOME_KEY = "doer_outcome"
#: ``{path: write|patch}`` for every file a write tool changed in this run.
FILES_KEY = "_doer_files"
#: Whether the last test run the Doer started through a shell passed.
TESTS_KEY = "_doer_tests_ok"
_LOG_MAX = 2000


def enabled() -> bool:
    return os.environ.get("AIFORGE_DOER_FINISH_ENDS_TURN", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def finish_summary(name: str, response) -> str:
    """The summary of a ``finish`` call that succeeded, else ""."""
    if name != "finish" or not isinstance(response, dict):
        return ""
    if response.get("ok") is not True or response.get("terminate") is not True:
        return ""
    return str(response.get("summary") or "").strip()


def _note_work(name: str, args, response, state) -> None:
    """Record what this call changed or proved. Only a call that reported
    success counts: a refused or malformed one changed nothing."""
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        return
    from aiforge_core.runtime.tools.mutating import PATCH_STYLE_TOOLS, writes_files
    args = args or {}
    if writes_files(name, args):
        if not response["ok"]:
            return
        from aiforge_core.runtime.scope_guard import _norm_path, _path_from_args
        files = dict(state.get(FILES_KEY) or {})
        action = "patch" if (name in PATCH_STYLE_TOOLS or name == "editor"
                             and args.get("command") != "create") else "write"
        for raw in _path_from_args(name, args):
            path = _norm_path(raw)
            if path:
                files.setdefault(path, action)
        if files != (state.get(FILES_KEY) or {}):
            state[FILES_KEY] = files
        return
    from aiforge_core.runtime.chat_agent._turn._outcomes import _is_test_run
    # A shell test run counts as green only when its exit code is the runner's
    # own (no pipe, no `;`, no `|| true`); `run_tests` has its own signal
    # (state['tests_ok']).
    if name != "run_tests" and _is_test_run(name, args) and (
            not response["ok"] or _own_exit_code(args)):
        state[TESTS_KEY] = response["ok"]


def _own_exit_code(args: dict) -> bool:
    cmd = str(args.get("cmd") or args.get("command") or "")
    return "|" not in cmd and ";" not in cmd


def _status(value) -> "str | None":
    return None if value is None else ("green" if value else "red")


def _contract(current) -> dict:
    """The model's closing words as a contract dict: its own JSON when it
    wrote one, else ``{"turn_log": <the text>}``."""
    if isinstance(current, dict):
        return dict(current)
    text = str(current or "").strip()
    body = text
    if body.startswith("```"):
        body = body.strip("`").strip()
        body = body[4:].strip() if body.lower().startswith("json") else body
    if body.startswith("{") and body.endswith("}"):
        try:
            data = json.loads(body)
            if isinstance(data, dict) and ({"turn_log", "file_diffs"} & set(data)):
                return data
        except ValueError:
            pass
    return {"turn_log": text[:_LOG_MAX]} if text else {}


def measured(state) -> dict:
    """What the run's own record says: files changed, checks run."""
    out: dict = {}
    files = state.get(FILES_KEY) or {}
    if files:
        out["file_diffs"] = [{"path": p, "action": a} for p, a in files.items()]
    tests = state.get("tests_ok")
    for key, value in (("compile_status", state.get("typecheck_ok")),
                       ("test_status",
                        state.get(TESTS_KEY) if tests is None else tests)):
        if _status(value):
            out[key] = _status(value)
    return out


def outcome(current, state, status: str = "done") -> str:
    """The Doer's outcome contract as JSON: the model's closing words with the
    measured record laid over them."""
    data = _contract(current)
    facts = measured(state)
    log = data.pop("turn_log", "")
    data.update(facts)
    data["turn_log"] = log
    if status != "done":
        data["blocker"] = log
    order = ("file_diffs", "compile_status", "test_status", "turn_log", "blocker")
    return json.dumps({**{k: data[k] for k in order if k in data},
                       **{k: v for k, v in data.items() if k not in order}},
                      ensure_ascii=False)


def make_finish_callback():
    """An ADK ``after_tool_callback``. Returns None always — it never replaces
    the tool response, so the callbacks after it still run."""

    def _cb(*, tool, args, tool_context, tool_response, **_kw):
        try:
            if not enabled():
                return None
            name = getattr(tool, "name", "") or ""
            state = tool_context.state
            summary = finish_summary(name, tool_response)
            if not summary:
                _note_work(name, args, tool_response, state)
                return None
            tool_context.actions.skip_summarization = True
            state[OUTCOME_KEY] = outcome(
                summary, state, str(tool_response.get("status") or "done"))
        except Exception:  # noqa: BLE001 — never break a tool call
            pass
        return None

    return _cb


def make_outcome_callback():
    """An ADK ``after_agent_callback``: when the turn ended with text instead
    of ``finish``, lay the measured record over that text. Nothing measured →
    the outcome is left exactly as the model wrote it."""

    def _cb(*, callback_context, **_kw):
        try:
            if not enabled():
                return None
            state = callback_context.state
            if not measured(state):
                return None
            current = state.get(OUTCOME_KEY)
            merged = outcome(current, state)
            if merged != current:
                state[OUTCOME_KEY] = merged
        except Exception:  # noqa: BLE001 — never break the stage
            pass
        return None

    return _cb


__all__ = ["make_finish_callback", "make_outcome_callback", "finish_summary",
           "outcome", "measured", "enabled", "OUTCOME_KEY", "FILES_KEY",
           "TESTS_KEY"]
