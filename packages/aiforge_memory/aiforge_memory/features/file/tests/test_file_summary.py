"""L3 — per-file summary parser + skip rules."""
from __future__ import annotations

import json
from unittest.mock import patch

from aiforge_memory.features.file import extract as fs
from aiforge_memory.features.symbol.extract import WalkedFile, WalkedSymbol

GOOD_RESPONSE = json.dumps({
    "summary": "FastAPI HTTP service exposing /payments/process and /health.",
    "purpose_tags": ["payment-flow", "api", "fastapi"],
})


def _walked(path: str, lang: str = "python", parse_error: bool = False,
            with_symbols: bool = True) -> WalkedFile:
    syms = []
    if with_symbols:
        syms.append(WalkedSymbol(
            fqname=f"{path}::demo", kind="function",
            file_path=path, signature="def demo(): ...",
        ))
    return WalkedFile(repo="t", path=path, hash="h", lang=lang,
                      lines=10, symbols=syms, parse_error=parse_error)


def test_summarize_writes_summary_and_tags(tmp_path) -> None:
    p = tmp_path / "x.py"
    p.write_text("def f(): pass\n")
    walked = [_walked("x.py")]
    with patch.object(fs, "_call_llm", return_value=GOOD_RESPONSE):
        out = fs.summarize_files(walked, repo="t", repo_root=tmp_path)
    assert len(out) == 1
    assert out[0].summary.startswith("FastAPI")
    assert "payment-flow" in out[0].purpose_tags


def test_summarize_skips_parse_error(tmp_path) -> None:
    p = tmp_path / "broken.py"
    p.write_text("def f(:\n")
    walked = [_walked("broken.py", parse_error=True)]
    out = fs.summarize_files(walked, repo="t", repo_root=tmp_path)
    assert out[0].skipped_reason == "parse_error"
    assert not out[0].summary


def test_summarize_skips_too_large(tmp_path) -> None:
    p = tmp_path / "big.py"
    p.write_text("x = 0\n" * 100_000)   # ~600 KB
    walked = [_walked("big.py")]
    out = fs.summarize_files(walked, repo="t", repo_root=tmp_path)
    assert out[0].skipped_reason == "too_large"


def test_summarize_retries_on_invalid_then_succeeds(tmp_path) -> None:
    p = tmp_path / "x.py"
    p.write_text("def f(): pass\n")
    walked = [_walked("x.py")]
    with patch.object(fs, "_call_llm",
                      side_effect=["not json", GOOD_RESPONSE]):
        out = fs.summarize_files(walked, repo="t", repo_root=tmp_path)
    assert out[0].summary.startswith("FastAPI")


def test_summarize_skips_on_llm_error(tmp_path) -> None:
    p = tmp_path / "x.py"
    p.write_text("def f(): pass\n")
    walked = [_walked("x.py")]
    with patch.object(fs, "_call_llm", side_effect=RuntimeError("offline")):
        out = fs.summarize_files(walked, repo="t", repo_root=tmp_path)
    assert out[0].skipped_reason == "llm_error"
    assert not out[0].summary
