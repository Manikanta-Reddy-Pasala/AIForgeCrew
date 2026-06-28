"""Minimal stdio JSON-RPC LSP client.

Built for batch use — start server, open files, fire reference queries,
shut down. Not a full LSP runtime: skips workspace events, watches,
diagnostics, completions. Sufficient for resolving CALLS.

Wire format: LSP framing = ``Content-Length: N\\r\\n\\r\\n{json}``.
Reads + writes use raw bytes; the response stream is parsed by a
dedicated reader thread that pushes onto an asyncio-free queue keyed on
JSON-RPC request id.

All paths converted to ``file://`` URIs. All positions are zero-based
(LSP convention) — callers translate from 1-based source lines.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path


class LspError(Exception):
    """Raised when the server fails to start or returns an error."""


class LspClient:
    """One-LSP-server-per-instance. Use as a context manager."""

    def __init__(
        self,
        command: list[str],
        *,
        root_uri: str,
        initialization_options: dict | None = None,
        timeout: float = 30.0,
        server_name: str = "",
    ) -> None:
        self._command = list(command)
        self._root_uri = root_uri
        self._init_opts = initialization_options or {}
        self._timeout = timeout
        self._server_name = server_name or (command[0] if command else "lsp")
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._next_id = 1
        self._pending: dict[int, queue.Queue] = {}
        self._stopped = False

    # ---- lifecycle ----------------------------------------------------

    def __enter__(self) -> LspClient:
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def start(self) -> None:
        bin_name = self._command[0]
        if shutil.which(bin_name) is None:
            raise LspError(f"server binary not on PATH: {bin_name}")
        try:
            self._proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise LspError(f"failed to start {bin_name}: {exc}") from exc

        self._reader = threading.Thread(
            target=self._read_loop, name=f"lsp-reader-{self._server_name}",
            daemon=True,
        )
        self._reader.start()

        self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": self._root_uri,
            "capabilities": {
                "textDocument": {
                    "references": {"dynamicRegistration": False},
                    "definition": {"dynamicRegistration": False},
                    "synchronization": {
                        "didSave": False, "willSave": False,
                    },
                },
            },
            "initializationOptions": self._init_opts,
        })
        self.notify("initialized", {})

    def stop(self) -> None:
        if self._stopped or self._proc is None:
            return
        self._stopped = True
        try:
            self.request("shutdown", None, timeout=5.0)
            self.notify("exit", None)
        except Exception:  # noqa: BLE001 — best-effort
            pass
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None

    # ---- file lifecycle ----------------------------------------------

    def did_open(self, path: str | Path, *, language_id: str) -> None:
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LspError(f"can't read {path}: {exc}") from exc
        self.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": path_to_uri(path),
                "languageId": language_id,
                "version": 1,
                "text": text,
            },
        })

    def did_close(self, path: str | Path) -> None:
        self.notify("textDocument/didClose", {
            "textDocument": {"uri": path_to_uri(path)},
        })

    # ---- queries ------------------------------------------------------

    def references(
        self, path: str | Path, *, line: int, character: int,
        include_declaration: bool = False,
    ) -> list[dict]:
        """Returns LSP Location[] (zero-based ranges, file:// URIs).
        Empty list on error or unsupported."""
        result = self.request("textDocument/references", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": include_declaration},
        })
        return result if isinstance(result, list) else []

    def definition(
        self, path: str | Path, *, line: int, character: int,
    ) -> list[dict]:
        result = self.request("textDocument/definition", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": character},
        })
        if isinstance(result, dict):
            return [result]
        return result if isinstance(result, list) else []

    # ---- raw request/notify ------------------------------------------

    def request(self, method: str, params, *, timeout: float | None = None):
        if self._proc is None or self._proc.poll() is not None:
            raise LspError("server not running")
        rid = self._next_id
        self._next_id += 1
        self._pending[rid] = queue.Queue(maxsize=1)
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self._send(msg)

        deadline = time.time() + (timeout or self._timeout)
        while True:
            try:
                resp = self._pending[rid].get(timeout=0.5)
                self._pending.pop(rid, None)
                if "error" in resp:
                    raise LspError(
                        f"{method}: {resp['error'].get('message', resp['error'])}"
                    )
                return resp.get("result")
            except queue.Empty:
                if time.time() > deadline:
                    self._pending.pop(rid, None)
                    raise LspError(
                        f"{method}: timeout after {timeout or self._timeout}s"
                    ) from None

    def notify(self, method: str, params) -> None:
        if self._proc is None:
            return
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ---- wire ---------------------------------------------------------

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        try:
            assert self._proc and self._proc.stdin
            self._proc.stdin.write(header + body)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise LspError(f"send failed: {exc}") from exc

    def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        out = self._proc.stdout
        while True:
            try:
                msg = _read_one(out)
            except (LspError, OSError):
                break
            if msg is None:
                break
            rid = msg.get("id")
            if rid is not None and rid in self._pending:
                self._pending[rid].put(msg)
            # Server-to-client requests + notifications are ignored.


def _read_one(stream) -> dict | None:
    """Read one Content-Length-framed JSON-RPC message. None at EOF."""
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        try:
            k, v = line.decode("ascii", "replace").rstrip("\r\n").split(":", 1)
        except ValueError:
            continue
        headers[k.strip().lower()] = v.strip()
    n = int(headers.get("content-length", "0") or "0")
    if n <= 0:
        return None
    body = stream.read(n)
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None


def path_to_uri(path: str | Path) -> str:
    p = Path(path).resolve()
    return "file://" + urllib.parse.quote(str(p))


def uri_to_path(uri: str) -> str:
    if uri.startswith("file://"):
        return urllib.parse.unquote(uri[len("file://"):])
    return uri
