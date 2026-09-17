"""Streaming responses: assembling SSE chunks into a message, and reading a
plain or streamed HTTP response."""
from __future__ import annotations

import http.client
import io
import json
import urllib.error


def _pkg():
    """The parent module, looked up on each call so a name patched there is the
    one used here."""
    import aiforge_core.llm.client._http as package
    return package


class _StreamAssembler:
    """Folds OpenAI-style ``chat.completion.chunk`` events back into one
    ``chat.completion`` body, handing each text piece to the sink as it comes."""

    def __init__(self, sink) -> None:
        self.sink = sink
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.tools: dict[int, dict] = {}
        self.finish = None
        self.usage = None
        self.meta: dict = {}

    def _emit(self, kind: str, text: str) -> None:
        try:
            self.sink(kind, text)
        except Exception:  # noqa: BLE001 — a display hook never fails a call
            pass

    def _merge_tool(self, tc: dict) -> None:
        slot = self.tools.setdefault(int(tc.get("index", len(self.tools))), {
            "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        slot["function"]["name"] += fn.get("name") or ""
        slot["function"]["arguments"] += fn.get("arguments") or ""

    def feed(self, chunk: dict) -> None:
        _pkg()._raise_if_model_dropped(chunk)
        if chunk.get("error"):
            # A proxy whose upstream failed mid-stream sends an error chunk;
            # ignoring it returned half an answer as a finished one.
            err = chunk["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ConnectionError(f"stream error from the model server: {str(msg)[:200]}")
        for k in ("id", "model", "created"):
            if chunk.get(k) is not None:
                self.meta.setdefault(k, chunk[k])
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for ch in chunk.get("choices") or []:
            self._feed_choice(ch)

    def _feed_choice(self, ch: dict) -> None:
        d = ch.get("delta") or {}
        if d.get("content"):
            self.content.append(d["content"])
            self._emit("content", d["content"])
        r = d.get("reasoning_content") or d.get("reasoning")
        if r:
            self.reasoning.append(r)
            self._emit("reasoning", r)
        for tc in d.get("tool_calls") or []:
            self._merge_tool(tc)
        if ch.get("finish_reason"):
            self.finish = ch["finish_reason"]

    def body(self) -> dict:
        msg: dict = {"role": "assistant", "content": "".join(self.content)}
        if self.reasoning:
            msg["reasoning_content"] = "".join(self.reasoning)
        if self.tools:
            msg["tool_calls"] = [self.tools[i] for i in sorted(self.tools)]
            msg["content"] = msg["content"] or None
        out = {**self.meta, "object": "chat.completion",
               "choices": [{"index": 0, "message": msg,
                            "finish_reason": self.finish}]}
        if self.usage is not None:
            out["usage"] = self.usage
        return out


def _streaming_payload(payload: bytes) -> bytes:
    body = json.loads(payload)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    return json.dumps(body).encode()


# Marker attribute set on an exception whose prompt REACHED the model and was
# abandoned on a read timeout. Callers above the transport (the chat loop's own
# retry sweep) must not re-issue that completion: each attempt leaves another
# generation running on a box that already could not finish the first.
TIMEOUT_SHIPPED_ATTR = "aiforge_llm_timeout_shipped"


def _read_http_response(conn, url: str) -> dict:
    """Read + parse the response body. Mimics urllib's HTTPError on a >=400 so
    the retry classifier handles it identically, and treats a 200-OK error body
    as transient."""
    resp = conn.getresponse()
    data = resp.read()
    if resp.status >= 400:
        raise urllib.error.HTTPError(
            url, resp.status, resp.reason, resp.headers, io.BytesIO(data))
    body = json.loads(data)
    _pkg()._raise_if_model_dropped(body)   # 200-OK error body → transient
    return body


def _read_sse_response(conn, url: str, sink) -> dict:
    """Read a streamed completion, feeding each token to ``sink``, and return
    it reassembled as a normal body. A server that ignored ``stream`` and
    answered with plain JSON is read as such."""
    pkg = _pkg()
    resp = conn.getresponse()
    if resp.status >= 400:
        data = resp.read()
        raise urllib.error.HTTPError(
            url, resp.status, resp.reason, resp.headers, io.BytesIO(data))
    if "text/event-stream" not in (resp.getheader("Content-Type") or ""):
        body = json.loads(resp.read())
        pkg._raise_if_model_dropped(body)
        return body
    asm = _StreamAssembler(sink)
    asm._emit("start", "")          # a retried call starts its text afresh
    done = _pump_sse(resp, asm)
    if not done and asm.finish is None:
        # Cut off: no [DONE] and no finish_reason. Accepting the partial body
        # made half an answer the FINAL.
        raise ConnectionError("model stream ended before the answer was complete")
    body = asm.body()
    pkg._raise_if_model_dropped(body)
    return body


def _pump_sse(resp, asm: "_StreamAssembler") -> bool:
    """Feed every ``data:`` event to ``asm``; True when ``[DONE]`` arrived. A
    chunked read cut mid-way is a dropped connection (retryable), not the
    non-OSError IncompleteRead that escaped the retry classifier."""
    try:
        while True:
            line = resp.readline()
            if not line:
                return False
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                return True
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            asm.feed(chunk)
    except http.client.IncompleteRead as exc:
        raise ConnectionError("model stream was cut off mid-response") from exc


# Endpoints that answered a stream request with 400: asked unstreamed from then
# on, instead of paying two round trips on every call.
_NO_STREAM: set = set()
