"""L14 — LSP integration unit tests (no live server).

Live LSP tests require pyright-langserver/typescript-language-server on
PATH and would be marked with a custom `live_lsp` marker. The tests here
exercise the wire codec, adapter discovery, and the resolver's pure
ref-to-edge mapping.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from aiforge_memory.features.lsp import adapters, client, resolver
from aiforge_memory.features.symbol.extract import WalkedFile, WalkedSymbol

# ─── Wire codec ───────────────────────────────────────────────────────

def _frame(payload: dict) -> bytes:
    body = json.dumps(payload).encode()
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


def test_read_one_parses_framed_message():
    data = _frame({"jsonrpc": "2.0", "id": 1, "result": {"x": 1}})
    msg = client._read_one(io.BytesIO(data))
    assert msg == {"jsonrpc": "2.0", "id": 1, "result": {"x": 1}}


def test_read_one_returns_none_at_eof():
    assert client._read_one(io.BytesIO(b"")) is None


def test_read_one_handles_two_messages_in_a_row():
    data = _frame({"id": 1, "result": "a"}) + _frame({"id": 2, "result": "b"})
    stream = io.BytesIO(data)
    a = client._read_one(stream)
    b = client._read_one(stream)
    assert a["id"] == 1
    assert b["id"] == 2


def test_path_to_uri_round_trip(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("")
    uri = client.path_to_uri(p)
    assert uri.startswith("file://")
    back = client.uri_to_path(uri)
    assert Path(back) == p


# ─── Adapter discovery ────────────────────────────────────────────────

def test_adapter_for_python_picks_pyright_when_present(monkeypatch):
    monkeypatch.setattr(
        "aiforge_memory.features.lsp.adapters.shutil.which",
        lambda b: "/usr/local/bin/pyright-langserver"
        if b == "pyright-langserver" else None,
    )
    cmd, lid, _opts = adapters.adapter_for("python")
    assert cmd[0] == "pyright-langserver"
    assert lid == "python"


def test_adapter_for_returns_none_when_binary_missing(monkeypatch):
    monkeypatch.setattr(
        "aiforge_memory.features.lsp.adapters.shutil.which",
        lambda b: None,
    )
    assert adapters.adapter_for("python") is None
    assert adapters.adapter_for("typescript") is None


def test_adapter_for_unknown_lang_returns_none():
    assert adapters.adapter_for("brainfuck") is None


def test_available_servers_returns_dict(monkeypatch):
    monkeypatch.setattr(
        "aiforge_memory.features.lsp.adapters.shutil.which", lambda b: None,
    )
    out = adapters.available_servers()
    assert "python" in out
    assert "java" in out
    assert all(v is False for v in out.values())


# ─── resolver enclosing logic ─────────────────────────────────────────

def test_enclosing_smallest_span_wins():
    R = resolver._SymRange
    ranges = [
        R("file::OuterClass", "f.py", 1, 100),
        R("file::OuterClass::method", "f.py", 5, 20),  # smaller span
    ]
    # ranges sorted by span; resolver expects ascending span first
    sorted_r = sorted(ranges, key=lambda r: (r.line_end - r.line_start, r.line_start))
    enc = resolver._enclosing(sorted_r, line_one=10)
    assert enc.fqname == "file::OuterClass::method"


def test_enclosing_returns_none_when_outside():
    R = resolver._SymRange
    ranges = [R("a", "f", 10, 20)]
    assert resolver._enclosing(ranges, line_one=5) is None


def test_ref_to_edge_emits_correct_caller(tmp_path):
    """Synthesize a reference Location pointing inside a known caller
    range and check the produced CallEdge."""
    repo_root = tmp_path
    (repo_root / "f.py").write_text("a\nb\nc\n")
    sym_index = {
        "f.py": [
            resolver._SymRange(
                fqname="f.py::caller", file_path="f.py",
                line_start=1, line_end=5,
            ),
        ],
    }
    callee = WalkedSymbol(
        fqname="g.py::callee", kind="function", file_path="g.py",
        line_start=10, line_end=20,
    )
    ref = {
        "uri": client.path_to_uri(repo_root / "f.py"),
        "range": {"start": {"line": 1, "character": 0}},
    }
    edge = resolver._ref_to_edge(
        ref=ref, callee=callee, sym_index=sym_index,
        repo_root=repo_root, repo="r",
    )
    assert edge is not None
    assert edge.caller_fqname == "f.py::caller"
    assert edge.callee_fqname == "g.py::callee"
    assert edge.confidence == 1.0


def test_ref_to_edge_skips_self_reference(tmp_path):
    repo_root = tmp_path
    (repo_root / "f.py").write_text("a\n")
    sym_index = {
        "f.py": [resolver._SymRange("f.py::same", "f.py", 1, 5)],
    }
    callee = WalkedSymbol(fqname="f.py::same", kind="function",
                          file_path="f.py", line_start=1, line_end=5)
    ref = {
        "uri": client.path_to_uri(repo_root / "f.py"),
        "range": {"start": {"line": 0, "character": 0}},
    }
    edge = resolver._ref_to_edge(
        ref=ref, callee=callee, sym_index=sym_index,
        repo_root=repo_root, repo="r",
    )
    assert edge is None


def test_resolve_calls_returns_empty_when_no_adapter(monkeypatch, tmp_path):
    """End-to-end gate: with no LSP server installed, resolve_calls
    returns [] and never raises — tree-sitter heuristic remains in play."""
    monkeypatch.setattr(
        "aiforge_memory.features.lsp.adapters.shutil.which", lambda b: None,
    )
    walked = [
        WalkedFile(
            repo="r", path="x.py", hash="h", lang="python", lines=1,
            symbols=[WalkedSymbol(fqname="x.py::foo", kind="function",
                                  file_path="x.py", line_start=1, line_end=2)],
        )
    ]
    out = resolver.resolve_calls(walked, repo="r", repo_root=tmp_path)
    assert out == []
