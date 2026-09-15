"""The API, as this client sees it.

Every call is loopback and unauthenticated (the backend trusts 127.0.0.1), and
every streaming call is Server-Sent Events over a plain GET/POST — the same
routes the web UI uses, so nothing here needs server-side support.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

# A run can sit quiet through a long build, but the API sends a `ping` every few
# seconds, so silence past this is a dead stream rather than a slow one — the
# caller re-attaches instead of waiting forever.
STREAM_READ_TIMEOUT = 130.0


class Stalled(Exception):
    """The event stream went silent past every keepalive."""


class Busy(Exception):
    """A run is already in flight for this session (HTTP 409)."""


class ApiDown(Exception):
    """Nothing is listening, or it is not the API."""


class Client:
    def __init__(self, base_url: str, *, timeout: float = 15.0):
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self._base, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ── plain calls ────────────────────────────────────────────────────────

    def healthy(self, timeout: float = 1.5) -> bool:
        try:
            r = self._http.get("/api/health", timeout=timeout)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def sessions(self) -> list[dict[str, Any]]:
        return self._json("GET", "/api/chat/sessions")

    def create_session(self, box_cwd: str) -> dict[str, Any]:
        return self._json("POST", "/api/chat/sessions", json={"cwd": box_cwd})

    def models(self) -> Any:
        return self._json("GET", "/api/chat/models")

    def integration(self, kind: str) -> dict[str, Any]:
        return self._json("GET", f"/api/integrations/{kind}")

    def integration_set(self, kind: str, patch: dict[str, Any]) -> dict[str, Any]:
        return self._json("PUT", f"/api/integrations/{kind}", json=patch)

    def integration_test(self, kind: str) -> dict[str, Any]:
        return self._json("POST", f"/api/integrations/{kind}/test")

    def mounts(self) -> dict[str, Any]:
        return self._json("GET", "/api/runtime/mounts")

    def stop(self, session_id: int) -> dict[str, Any]:
        return self._json("POST", f"/api/chat/sessions/{session_id}/stop")

    def steer(self, session_id: int, text: str) -> dict[str, Any]:
        return self._json("POST", f"/api/chat/sessions/{session_id}/steer",
                          json={"text": text})

    def approve(self, session_id: int, approval_id: Any, decision: str,
                note: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"id": approval_id, "decision": decision}
        if note:
            body["note"] = note
        return self._json("POST", f"/api/chat/sessions/{session_id}/approve", json=body)

    def kill_all(self) -> dict[str, Any]:
        return self._json("POST", "/api/chat/kill-all")

    def compact(self, session_id: int) -> dict[str, Any]:
        return self._json("POST", f"/api/chat/sessions/{session_id}/compact")

    def llm_usage(self, session_id: int) -> dict[str, Any]:
        return self._json("GET", f"/api/chat/sessions/{session_id}/llm-usage")

    def messages(self, session_id: int) -> dict[str, Any]:
        return self._json("GET", f"/api/chat/sessions/{session_id}")

    # ── streams ────────────────────────────────────────────────────────────

    def send(self, session_id: int, content: str, *, mode: str = "simple",
             quick: bool = False, review_edits: bool = False) -> Iterator[dict[str, Any]]:
        """Post a turn and yield its events as they arrive.

        A 409 means something else is already running this session (a second
        terminal, the web UI): the caller attaches to that run instead of
        starting a rival producer, which the server would refuse anyway.
        """
        body = {"content": content, "mode": mode, "quick": quick,
                "review_edits": review_edits}
        yield from self._sse("POST", f"/api/chat/sessions/{session_id}/message", json=body)

    def attach(self, session_id: int) -> Iterator[dict[str, Any]]:
        """Replay an in-flight run's buffered events, then tail it live."""
        yield from self._sse("GET", f"/api/chat/sessions/{session_id}/attach")

    # ── internals ──────────────────────────────────────────────────────────

    def _json(self, method: str, path: str, **kw) -> Any:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise ApiDown(str(exc)) from exc
        if r.status_code == 409:
            raise Busy(_detail(r))
        r.raise_for_status()
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    def _sse(self, method: str, path: str, **kw) -> Iterator[dict[str, Any]]:
        timeout = httpx.Timeout(15.0, read=STREAM_READ_TIMEOUT)
        try:
            with self._http.stream(method, path, timeout=timeout,
                                   headers={"Accept": "text/event-stream"}, **kw) as r:
                if r.status_code == 409:
                    r.read()
                    raise Busy(_detail(r))
                if r.status_code != 200:
                    r.read()
                    raise ApiDown(f"{r.status_code} {_detail(r)}")
                for line in r.iter_lines():
                    ev = parse_sse_line(line)
                    if ev is not None:
                        yield ev
        except httpx.ReadTimeout as exc:
            raise Stalled(f"no events for {STREAM_READ_TIMEOUT:.0f}s") from exc
        except httpx.HTTPError as exc:
            raise Stalled(str(exc)) from exc


def parse_sse_line(line: str) -> dict[str, Any] | None:
    """One SSE ``data:`` line to an event, or None for framing.

    The API sends exactly one JSON object per data line, so no multi-line
    reassembly is needed — but a line that is not JSON is framing or a comment
    and must not crash a two-hour run.
    """
    if not line or not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload:
        return None
    try:
        ev = json.loads(payload)
    except ValueError:
        return None
    return ev if isinstance(ev, dict) else None


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("detail") or body.get("error") or body)
        return str(body)
    except ValueError:
        return (response.text or "").strip()[:200]
