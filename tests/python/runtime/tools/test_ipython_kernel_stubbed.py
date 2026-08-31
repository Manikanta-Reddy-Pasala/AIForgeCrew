"""The persistent notebook kernel, driven against a fake jupyter_client.

jupyter_client is optional, so on most boxes this whole tool answers
``kernel_missing`` — which is exactly why its real behaviour needs stubbing to
be testable at all. Three things here are the scars:

  * one kernel per RUN, not per call. A fresh uuid per call span a new kernel
    for every cell (so nothing persisted) and leaked it, because destroy keys
    on the run's session id and never matched the throwaway uuid.
  * the drain deadline is WALL CLOCK, not per-message. A per-message idle
    timeout is reset by every print, so ``while True: print(x)`` never ends.
  * any stderr means the cell errored. The old ``and not stdout`` check
    reported a cell that printed and THEN raised as a success.
"""
from __future__ import annotations

import queue
import types as pytypes

import pytest

from aiforge_core.runtime.tools import ipython_kernel as K


def _msg(mtype, content, msg_id="m1"):
    return {"msg_type": mtype, "content": content,
            "parent_header": {"msg_id": msg_id}}


class _Client:
    """A kernel client replaying a scripted iopub stream."""

    def __init__(self, msgs=None):
        self.msgs = list(msgs or [])
        self.executed: list = []
        self.channels = "open"

    def start_channels(self):
        self.channels = "open"

    def stop_channels(self):
        self.channels = "closed"

    def wait_for_ready(self, timeout=None):
        return True

    def execute(self, code, silent=False, store_history=True):
        self.executed.append(code)
        return "m1"

    def get_iopub_msg(self, timeout=None):
        if not self.msgs:
            raise queue.Empty
        return self.msgs.pop(0)


class _KM:
    def __init__(self, client):
        self._client = client
        self.started = False
        self.shutdown = None
        self.interrupted = 0

    def start_kernel(self):
        self.started = True

    def client(self):
        return self._client

    def shutdown_kernel(self, now=False):
        self.shutdown = now

    def interrupt_kernel(self):
        self.interrupted += 1


@pytest.fixture()
def kernel(monkeypatch):
    """A booted fake kernel bound to run id 'r1'."""
    client = _Client()
    km = _KM(client)
    monkeypatch.setattr(K, "_jupyter_available", lambda: True)
    monkeypatch.setattr(K, "_start_kernel", lambda rid: (km, client))
    K._kernels.clear()
    K._clients.clear()
    yield pytypes.SimpleNamespace(km=km, client=client)
    K._kernels.clear()
    K._clients.clear()


# ─── one kernel per run ────────────────────────────────────────────────


def test_a_call_with_no_run_shares_one_stable_kernel():
    """A fresh uuid per call spawned a kernel per cell and leaked every one."""
    K.set_run_id(None)
    assert K._effective_run_id(None) == "default"


def test_the_runner_binds_the_kernel_for_the_whole_run():
    K.set_run_id("adk-9")
    try:
        assert K._effective_run_id(None) == "adk-9"
        assert K._effective_run_id("mine") == "mine"
    finally:
        K.set_run_id(None)


def test_the_kernel_is_booted_once_and_reused(monkeypatch):
    client = _Client()
    km = _KM(client)
    boots: list = []

    class _KMFactory:
        def __new__(cls):
            boots.append(1)
            return km
    mod = pytypes.SimpleNamespace(KernelManager=_KMFactory)
    monkeypatch.setitem(__import__("sys").modules, "jupyter_client",
                        pytypes.SimpleNamespace(manager=mod))
    monkeypatch.setitem(__import__("sys").modules, "jupyter_client.manager", mod)
    K._kernels.clear()
    K._clients.clear()
    try:
        assert K._start_kernel("r1") == (km, client)
        assert K._start_kernel("r1") == (km, client)
        assert boots == [1] and km.started is True
        assert client.executed, "the agentskills helpers are injected"
    finally:
        K._kernels.clear()
        K._clients.clear()


def test_a_kernel_is_shut_down_and_forgotten(kernel):
    K._kernels["r1"] = kernel.km
    K._clients["r1"] = kernel.client
    K.destroy_kernel("r1")
    assert kernel.client.channels == "closed" and kernel.km.shutdown is True
    assert "r1" not in K._kernels and "r1" not in K._clients


def test_destroying_a_kernel_that_never_started_is_quiet(kernel):
    K.destroy_kernel("nope")


def test_a_kernel_that_will_not_die_does_not_raise(kernel):
    kernel.km.shutdown_kernel = lambda now=False: (_ for _ in ()).throw(
        RuntimeError("zmq"))
    kernel.client.stop_channels = lambda: (_ for _ in ()).throw(OSError("x"))
    K._kernels["r1"] = kernel.km
    K._clients["r1"] = kernel.client
    K.destroy_kernel("r1")


# ─── folding the iopub stream ──────────────────────────────────────────


def _fold(msgs):
    acc: dict = {"stdout": [], "stderr": [], "result": ""}
    for mtype, content in msgs:
        if K._fold_iopub_msg(content, mtype, acc):
            break
    return acc


def test_stdout_and_stderr_are_kept_apart():
    acc = _fold([("stream", {"name": "stdout", "text": "hi"}),
                 ("stream", {"name": "stderr", "text": "warn"})])
    assert acc["stdout"] == ["hi"] and acc["stderr"] == ["warn"]


def test_a_cells_value_is_its_plain_text_repr():
    acc = _fold([("execute_result", {"data": {"text/plain": "42"}})])
    assert acc["result"] == "42"


def test_a_traceback_lands_on_stderr():
    acc = _fold([("error", {"traceback": ["Traceback…", "ValueError: x"]})])
    assert acc["stderr"] == ["Traceback…\nValueError: x"]


def test_going_idle_stops_the_drain():
    assert K._fold_iopub_msg({"execution_state": "idle"}, "status", {}) is True
    assert K._fold_iopub_msg({"execution_state": "busy"}, "status",
                             {"stdout": []}) is False


def test_a_message_from_another_cell_is_ignored():
    client = _Client([_msg("stream", {"name": "stdout", "text": "old"},
                           msg_id="other"),
                      _msg("stream", {"name": "stdout", "text": "mine"}),
                      _msg("status", {"execution_state": "idle"})])
    assert K._drain_iopub(client, "m1", timeout=5)["stdout"] == "mine"


def test_a_cell_that_prints_forever_still_ends(monkeypatch):
    """The deadline is wall clock: a per-message idle timeout is reset by
    every print, so `while True: print(x)` would never terminate."""
    class _Endless(_Client):
        def get_iopub_msg(self, timeout=None):
            return _msg("stream", {"name": "stdout", "text": "x"})
    ticks = iter([0.0, 0.0, 1.0, 99.0])
    import time as _t
    monkeypatch.setattr(_t, "monotonic", lambda: next(ticks))
    out = K._drain_iopub(_Endless(), "m1", timeout=5)
    assert out["timed_out"] is True, "the wall clock ends it, the prints do not"
    assert out["stdout"] == "xx", "whatever it printed until then is kept"


def test_a_silent_kernel_times_out():
    out = K._drain_iopub(_Client([]), "m1", timeout=1)
    assert out["timed_out"] is True and out["stdout"] == ""


# ─── running a cell ────────────────────────────────────────────────────


def _run(kernel, msgs, **kw):
    kernel.client.msgs = list(msgs)
    return K.execute_ipython_cell("print('hi')", _run_id="r1", **kw)


def test_a_cell_returns_what_it_printed(kernel):
    res = _run(kernel, [_msg("stream", {"name": "stdout", "text": "hi\n"}),
                        _msg("execute_result", {"data": {"text/plain": "7"}}),
                        _msg("status", {"execution_state": "idle"})])
    assert res["ok"] is True and res["stdout"] == "hi\n" and res["result"] == "7"
    assert res["truncated"] is False


def test_a_cell_that_printed_then_raised_is_not_a_success(kernel):
    """The old `stderr and not stdout` check called this OK."""
    res = _run(kernel, [_msg("stream", {"name": "stdout", "text": "step 1\n"}),
                        _msg("error", {"traceback": ["ValueError: x"]}),
                        _msg("status", {"execution_state": "idle"})])
    assert res["ok"] is False and res["stdout"] == "step 1\n"
    assert "ValueError" in res["stderr"]


def test_a_runaway_cell_is_interrupted_so_it_stops_burning_the_kernel(kernel):
    res = _run(kernel, [], timeout=1)
    assert res["error"] == "timeout" and res["truncated"] is True
    assert kernel.km.interrupted == 1


def test_an_uninterruptible_kernel_still_answers(kernel):
    kernel.km.interrupt_kernel = lambda: (_ for _ in ()).throw(OSError("zmq"))
    assert _run(kernel, [], timeout=1)["error"] == "timeout"


def test_a_huge_output_is_capped(kernel):
    big = "z" * (K._STDOUT_CAP_BYTES + 100)
    res = _run(kernel, [_msg("stream", {"name": "stdout", "text": big}),
                        _msg("status", {"execution_state": "idle"})])
    assert len(res["stdout"]) == K._STDOUT_CAP_BYTES and res["truncated"] is True


def test_an_empty_cell_never_reaches_the_kernel(kernel):
    assert K.execute_ipython_cell("   ") == {"ok": False, "error": "empty_code"}
    assert kernel.client.executed == []


def test_without_jupyter_the_tool_says_so_instead_of_crashing(monkeypatch):
    """The agent loop has to survive on a box without the optional install."""
    monkeypatch.setattr(K, "_jupyter_available", lambda: False)
    res = K.execute_ipython_cell("1+1")
    assert res == {"ok": False, "error": "kernel_missing",
                   "hint": "pip install jupyter_client ipykernel"}


def test_a_kernel_that_will_not_boot_is_a_soft_error(monkeypatch):
    monkeypatch.setattr(K, "_jupyter_available", lambda: True)
    monkeypatch.setattr(K, "_start_kernel",
                        lambda rid: (_ for _ in ()).throw(OSError("no port")))
    res = K.execute_ipython_cell("1+1")
    assert res["error"] == "kernel_boot_failed" and "no port" in res["detail"]


def test_a_dead_kernel_mid_cell_is_a_soft_error(kernel):
    kernel.client.execute = lambda code, **kw: (_ for _ in ()).throw(
        RuntimeError("zmq closed"))
    res = K.execute_ipython_cell("1+1", _run_id="r1")
    assert res["error"] == "cell_exec_failed" and "zmq closed" in res["detail"]


def test_the_optional_import_is_what_decides_availability(monkeypatch):
    import builtins
    real = builtins.__import__

    def _imp(name, *a, **k):
        if name == "jupyter_client":
            raise ImportError("nope")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _imp)
    assert K._jupyter_available() is False
