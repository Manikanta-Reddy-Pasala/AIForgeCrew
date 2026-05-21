"""Persistent IPython/Jupyter kernel tool for the Doer (OH parity sub #3).

Single entry point :func:`execute_ipython_cell`. One ``KernelManager`` per
ADK run_id; variables / imports / function defs persist across calls.
Destroyed in :func:`destroy_kernel` from the runner finally block.

Soft-error contract. ``jupyter_client`` is an optional dependency: when
absent, every call returns ``{ok: False, error: "kernel_missing"}`` so the
agent loop survives on boxes without the install.
"""
from __future__ import annotations

import uuid
from typing import Any

from ._trace import emit

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


def _drain_iopub(client: Any, msg_id: str, timeout: int) -> dict[str, Any]:
    """Collect stdout / stderr / result from a single execute request."""
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    result: str = ""
    timed_out = False
    while True:
        try:
            msg = client.get_iopub_msg(timeout=timeout)
        except Exception:  # noqa: BLE001 — Empty / queue timeout
            timed_out = True
            break
        if msg.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        mtype = msg.get("msg_type", "")
        content = msg.get("content", {})
        if mtype == "stream":
            name = content.get("name", "")
            txt = content.get("text", "")
            if name == "stderr":
                stderr_parts.append(txt)
            else:
                stdout_parts.append(txt)
        elif mtype == "execute_result":
            data = content.get("data", {})
            result = str(data.get("text/plain", ""))
        elif mtype == "error":
            tb = "\n".join(content.get("traceback", []))
            stderr_parts.append(tb)
        elif mtype == "status" and content.get("execution_state") == "idle":
            break
    return {
        "stdout": "".join(stdout_parts),
        "stderr": "".join(stderr_parts),
        "result": result,
        "timed_out": timed_out,
    }


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
    if _run_id is None:
        _run_id = "default-" + uuid.uuid4().hex[:8]

    try:
        _km, client = _start_kernel(_run_id)
    except Exception as exc:  # noqa: BLE001 — kernel boot failures
        return {"ok": False, "error": "kernel_boot_failed",
                "detail": str(exc)[:300]}

    msg_id = client.execute(code)
    drained = _drain_iopub(client, msg_id, timeout)
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
    has_error = bool(stderr) and not stdout
    emit("Cell", {"action": "executed", "ok": not has_error,
                  "bytes": len(stdout) + len(stderr)})
    return {
        "ok": not has_error,
        "stdout": stdout, "stderr": stderr,
        "result": drained["result"],
        "truncated": truncated,
    }
