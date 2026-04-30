"""L5 — chunk-and-embed unit tests with mocked sidecar."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from aiforge_core.codemem.ingest import embed as em
from aiforge_core.codemem.ingest.treesitter_walk import WalkedFile, WalkedSymbol


FIX = Path(__file__).parent.parent / "L4_symbols" / "fixtures" / "poly_repo"


def _walked(path: str, lang: str = "python") -> WalkedFile:
    return WalkedFile(
        repo="t", path=path, hash="h", lang=lang, lines=10,
        symbols=[WalkedSymbol(fqname=f"{path}::demo", kind="function",
                              file_path=path, signature="def demo(): ...")]
    )


def test_split_short_file_one_chunk(tmp_path) -> None:
    chunks = em._split("a\nb\nc\n", file_path="x.py")
    assert len(chunks) == 1
    assert chunks[0][0] == 0  # idx
    assert "a" in chunks[0][1]


def test_split_long_file_multiple_chunks() -> None:
    text = "\n".join(f"line{i}" for i in range(150))
    chunks = em._split(text, file_path="long.py")
    assert len(chunks) >= 3
    # Each chunk has ≤ CHUNK_LINES lines
    for _, ch_text, _, _ in chunks:
        assert ch_text.count("\n") + 1 <= em.CHUNK_LINES


def test_chunk_id_is_stable() -> None:
    a = em._chunk_id("r", "x.py", 0)
    b = em._chunk_id("r", "x.py", 0)
    assert a == b
    assert a != em._chunk_id("r", "x.py", 1)
    assert len(a) == 32


def test_chunk_and_embed_calls_sidecar(tmp_path) -> None:
    p = tmp_path / "x.py"
    p.write_text("def f(): pass\n" * 5)
    walked = [_walked("x.py")]
    fake_vec = [0.1] * 1024
    with patch.object(em, "_embed", return_value=fake_vec) as mock_embed:
        chunks = em.chunk_and_embed(walked, repo="t", repo_root=tmp_path)
    assert len(chunks) >= 1
    assert chunks[0].embed_vec == fake_vec
    assert chunks[0].repo == "t"
    assert chunks[0].file_path == "x.py"
    assert mock_embed.called


def test_chunk_and_embed_skips_too_large(tmp_path) -> None:
    p = tmp_path / "big.py"
    p.write_text("x = 0\n" * 100_000)
    walked = [_walked("big.py")]
    chunks = em.chunk_and_embed(walked, repo="t", repo_root=tmp_path)
    assert chunks == []


def test_chunk_and_embed_skips_parse_error(tmp_path) -> None:
    p = tmp_path / "x.py"
    p.write_text("def f(): pass\n")
    walked = [WalkedFile(
        repo="t", path="x.py", hash="h", lang="python",
        lines=1, parse_error=True,
    )]
    chunks = em.chunk_and_embed(walked, repo="t", repo_root=tmp_path)
    assert chunks == []


def test_sidecar_failure_skips_remaining_chunks(tmp_path) -> None:
    p = tmp_path / "x.py"
    p.write_text("\n".join(f"l{i}" for i in range(150)))
    walked = [_walked("x.py")]
    with patch.object(em, "_embed", side_effect=RuntimeError("sidecar down")):
        chunks = em.chunk_and_embed(walked, repo="t", repo_root=tmp_path)
    assert chunks == []
