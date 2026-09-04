"""Persistent IPython/Jupyter kernel tool for the Doer (OH parity sub #3).

Single entry point :func:`execute_ipython_cell`. One ``KernelManager`` per
ADK run_id; variables / imports / function defs persist across calls.
Destroyed in :func:`destroy_kernel` from the runner finally block.

Soft-error contract. ``jupyter_client`` is an optional dependency: when
absent, every call returns ``{ok: False, error: "kernel_missing"}`` so the
agent loop survives on boxes without the install.
"""
from __future__ import annotations

import contextvars
import uuid
from typing import Any

from ._trace import emit

# The run a tool call belongs to. ADK FunctionTools don't forward ``_run_id``
# (the Doer wrapper omits it), so — like bash — fall back to a contextvar the
# runner sets, then a STABLE "default". A fresh uuid per call spawned a new
# kernel each cell (no state persistence) and leaked it (destroy keys on the
# run's session id, which never matched the throwaway uuid).
_RUN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ipython_run_id", default=None)


def set_run_id(run_id: str | None) -> None:
    _RUN_ID.set(run_id)


def _effective_run_id(explicit: str | None) -> str:
    return explicit or _RUN_ID.get() or "default"

_STDOUT_CAP_BYTES = 8000
_DEFAULT_TIMEOUT_S = 60

_kernels: dict[str, Any] = {}
_clients: dict[str, Any] = {}


def _jupyter_available() -> bool:
    try:
        import jupyter_client  # noqa: F401
        return True
    except ImportError:
        return False


def _start_kernel(run_id: str) -> tuple[Any, Any]:
    if run_id in _kernels:
        return _kernels[run_id], _clients[run_id]
    from jupyter_client.manager import KernelManager
    km = KernelManager()
    km.start_kernel()
    client = km.client()
    client.start_channels()
    client.wait_for_ready(timeout=10)
    _kernels[run_id] = km
    _clients[run_id] = client
    emit("Cell", {"action": "kernel_started", "run_id": run_id})
    # Egress guard FIRST — before AgentSkills, before any user cell. A cell
    # that reaches the network with an HTTP library was the transport an agent
    # switched to after web_fetch and then curl were both refused, and it
    # worked. See runtime/tools/kernel_egress.py.
    try:
        from .kernel_egress import guard_source
        src = guard_source()
        if src:
            client.execute(src, silent=True, store_history=False)
            _drain_iopub(client, "", timeout=5)
    except Exception as exc:  # noqa: BLE001 — a guard never breaks the kernel
        emit("Cell", {"action": "egress_guard_failed", "detail": str(exc)[:200]})
    # Inject AgentSkills helpers (sub #12) so the model can call
    # open_file / goto_line / find_file / search_dir / search_file /
    # create_file / run_cmd from any cell.
    try:
        from .agentskills import bootstrap_code
        client.execute(bootstrap_code(), silent=True, store_history=False)
        # Drain the bootstrap iopub events so they don't pollute the
        # next real call's output.
        _drain_iopub(client, "", timeout=5)
    except Exception as exc:  # noqa: BLE001 — best-effort
        emit("Cell", {"action": "agentskills_inject_failed",
                      "error": str(exc)[:200]})
    return km, client


def destroy_kernel(run_id: str) -> None:
    """Shut down the kernel for ``run_id`` (best-effort)."""
    client = _clients.pop(run_id, None)
    km = _kernels.pop(run_id, None)
    if client is not None:
        try:
            client.stop_channels()
        except Exception:  # noqa: BLE001
            pass
    if km is not None:
        try:
            km.shutdown_kernel(now=True)
        except Exception:  # noqa: BLE001
            pass
        emit("Cell", {"action": "kernel_stopped", "run_id": run_id})


def _fold_iopub_msg(content: dict, mtype: str, acc: dict) -> bool:
    """Fold one iopub message into the stdout/stderr/result accumulators.
    Returns True when it signals the kernel is idle (stop draining)."""
    if mtype == "stream":
        target = "stderr" if content.get("name", "") == "stderr" else "stdout"
        acc[target].append(content.get("text", ""))
    elif mtype == "execute_result":
        acc["result"] = str(content.get("data", {}).get("text/plain", ""))
    elif mtype == "error":
        acc["stderr"].append("\n".join(content.get("traceback", [])))
    elif mtype == "status" and content.get("execution_state") == "idle":
        return True
    return False


def _drain_iopub(client: Any, msg_id: str, timeout: int) -> dict[str, Any]:
    """Collect stdout / stderr / result from a single execute request."""
    import time as _t
    acc: dict = {"stdout": [], "stderr": [], "result": ""}
    timed_out = False
    # WALL-CLOCK deadline — not a per-message idle timeout. Otherwise a cell that
    # prints continuously (``while True: print(x)``) resets the timeout on every
    # message and never terminates.
    deadline = _t.monotonic() + max(1, timeout)
    while True:
        remaining = deadline - _t.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            msg = client.get_iopub_msg(timeout=min(timeout, remaining))
        except Exception:  # noqa: BLE001 — Empty / queue timeout
            timed_out = True
            break
        if msg.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        if _fold_iopub_msg(msg.get("content", {}), msg.get("msg_type", ""), acc):
            break
    return {
        "stdout": "".join(acc["stdout"]),
        "stderr": "".join(acc["stderr"]),
        "result": acc["result"],
        "timed_out": timed_out,
    }


def _sandbox_refusal() -> dict[str, Any] | None:
    """``None`` unless the operator demanded a sandbox this tool cannot provide."""
    try:
        from aiforge_core.runtime import docker_sandbox
        if docker_sandbox.sandbox_policy() != "required":
            return None
    except Exception:  # noqa: BLE001 — no sandbox module → nothing to enforce
        return None
    return {"ok": False, "error": "sandbox_required",
            "hint": ("AIFORGE_SANDBOX_REQUIRED=1 forbids host execution, and "
                     "the IPython kernel runs in-process on the host — the "
                     "Docker sandbox covers `bash` only. Use bash for work "
                     "that must be contained, or unset the requirement.")}


def execute_ipython_cell(
    code: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT_S,
    _run_id: str | None = None,
) -> dict[str, Any]:
    """Run ``code`` in the persistent IPython kernel for ``_run_id``."""
    if not code or not code.strip():
        return {"ok": False, "error": "empty_code"}
    if not _jupyter_available():
        return {"ok": False, "error": "kernel_missing",
                "hint": "pip install jupyter_client ipykernel"}
    # The Docker sandbox routes ``bash`` only — this kernel starts in-process,
    # on the host, whatever the sandbox policy says. Under REQUIRED that is a
    # silent host fallback, which is the one thing REQUIRED exists to forbid,
    # so refuse and say why rather than running the cell.
    refusal = _sandbox_refusal()
    if refusal is not None:
        return refusal
    _run_id = _effective_run_id(_run_id)

    try:
        _km, client = _start_kernel(_run_id)
    except Exception as exc:  # noqa: BLE001 — kernel boot failures
        return {"ok": False, "error": "kernel_boot_failed",
                "detail": str(exc)[:300]}

    try:
        msg_id = client.execute(code)
        drained = _drain_iopub(client, msg_id, timeout)
    except Exception as exc:  # noqa: BLE001 — dead kernel / ZMQ error
        return {"ok": False, "error": "cell_exec_failed",
                "detail": str(exc)[:300]}
    if drained.get("timed_out"):
        # Stop a runaway cell so it doesn't keep burning the kernel.
        try:
            _km.interrupt_kernel()
        except Exception:  # noqa: BLE001
            pass
    stdout = drained["stdout"][:_STDOUT_CAP_BYTES]
    stderr = drained["stderr"][:_STDOUT_CAP_BYTES]
    truncated = (
        len(drained["stdout"]) > _STDOUT_CAP_BYTES
        or len(drained["stderr"]) > _STDOUT_CAP_BYTES
    )
    if drained["timed_out"]:
        return {
            "ok": False, "error": "timeout",
            "stdout": stdout, "stderr": stderr, "truncated": True,
        }
    # Any stderr means the cell errored. The old `and not stdout` wrongly
    # reported a cell that PRINTED then raised (stdout + stderr both set) as OK.
    has_error = bool(stderr)
    emit("Cell", {"action": "executed", "ok": not has_error,
                  "bytes": len(stdout) + len(stderr)})
    return {
        "ok": not has_error,
        "stdout": stdout, "stderr": stderr,
        "result": drained["result"],
        "truncated": truncated,
    }
