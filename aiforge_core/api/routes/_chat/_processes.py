"""The Processes dock: a chat's running commands and their live logs.

Read-only views over :mod:`aiforge_core.runtime.proc_logs`, plus Stop for one
command. The web UI polls the list while the chat is open and tails a job by
passing the byte offsets it already has, so each poll carries only new output.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from ._core import router


def _session_or_404(session_id: int) -> None:
    from aiforge_core.runtime import chat_store
    if not chat_store.get_session(session_id):
        raise HTTPException(404, f"session {session_id} not found")


def _offsets(raw: str | None) -> list[int]:
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            raise HTTPException(400, f"bad offset {part!r}") from None
    return out


@router.get("/api/chat/sessions/{session_id}/processes",
            responses={404: {"description": "Not found"}})
def chat_processes(session_id: int) -> dict:
    """The chat's commands: running ones first, then the last few that
    ended. ``shown`` > 0 means the agent asked to show that log (the higher,
    the more recent the ask)."""
    _session_or_404(session_id)
    from aiforge_core.runtime import proc_logs
    return {"processes": proc_logs.list_for_session(session_id)}


@router.get("/api/chat/sessions/{session_id}/processes/{key}/log",
            responses={400: {"description": "Bad offset"},
                       404: {"description": "Not found"}})
def chat_process_log(session_id: int, key: str,
                     offsets: str | None = Query(None, description=(
                         "comma-separated byte offset per stream (stdout,"
                         "stderr); negative = the last N bytes")),
                     max_bytes: int = Query(64 * 1024, ge=1024,
                                            le=256 * 1024)) -> dict:
    """Output of one command from ``offsets`` on, per stream. Pass each
    stream's ``next`` back as its offset to get only what is new."""
    _session_or_404(session_id)
    from aiforge_core.runtime import proc_logs
    res = proc_logs.read(session_id, key, _offsets(offsets), max_bytes)
    if res is None:
        raise HTTPException(404, f"no process {key!r} in session {session_id}")
    return res


@router.post("/api/chat/sessions/{session_id}/processes/{key}/kill",
             responses={404: {"description": "Not found"}})
def chat_process_kill(session_id: int, key: str) -> dict:
    """Stop one running command of the chat."""
    _session_or_404(session_id)
    from aiforge_core.runtime import proc_logs
    if not proc_logs.kill(session_id, key):
        raise HTTPException(404, f"no running process {key!r} in session "
                                 f"{session_id}")
    return {"ok": True, "id": key}


@router.get("/api/chat/sessions/{session_id}/processes/{key}/file",
            responses={404: {"description": "Not found"}})
def chat_process_file(session_id: int, key: str,
                      path: str = Query(..., description=(
                          "one of the job's log_files")),
                      offset: int = Query(-64 * 1024, description=(
                          "byte offset; negative = the last N bytes")),
                      max_bytes: int = Query(64 * 1024, ge=1024,
                                             le=256 * 1024)) -> dict:
    """A log file the command writes (or follows), from ``offset`` on. Only
    files listed in that command's ``log_files``."""
    _session_or_404(session_id)
    from aiforge_core.runtime import proc_logs
    res = proc_logs.read_file(session_id, key, path, offset, max_bytes)
    if res is None:
        raise HTTPException(404, f"{path!r} is not a log file of {key!r}")
    return res


def _download_name(text: str) -> str:
    keep = "".join(c if c.isalnum() or c in "._-" else "_" for c in text)
    return (keep.strip("._") or "log")[:80]


@router.get("/api/chat/sessions/{session_id}/processes/{key}/download",
            responses={404: {"description": "Not found"}})
def chat_process_download(session_id: int, key: str,
                          path: str | None = Query(None, description=(
                              "one of the job's log_files; omitted = the "
                              "command's own output"))):
    """The whole log as a file: a log file of the command, or its own output
    (stdout, then stderr)."""
    _session_or_404(session_id)
    from aiforge_core.runtime import proc_logs
    if path:
        real = proc_logs.file_path(session_id, key, path)
        if real is None:
            raise HTTPException(404, f"{path!r} is not a log file of {key!r}")
        return FileResponse(real, media_type="text/plain",
                            filename=_download_name(os.path.basename(real)))
    chunks = proc_logs.output_chunks(session_id, key)
    if chunks is None:
        raise HTTPException(404, f"no process {key!r} in session {session_id}")
    name = _download_name(f"chat-{session_id}-{key}") + ".log"
    return StreamingResponse(
        chunks, media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})
