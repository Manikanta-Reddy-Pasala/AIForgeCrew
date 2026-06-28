"""L1 — RepoMix wrapper: shell out, return text + sha256, soft-fail."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from aiforge_memory.features.repo import pack_repo as pack_repo

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tiny_repo"


def test_pack_returns_text_and_sha_when_repomix_present() -> None:
    """Mocked subprocess: we don't require repomix on PATH for unit tests."""
    fake_stdout = "# tiny_repo pack\n## File: src/main.py\n"
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = fake_stdout
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""
        text, sha = pack_repo.pack(FIXTURE_DIR)
    assert text == fake_stdout
    assert len(sha) == 64  # sha256 hex
    # Hashing same input twice yields same sha
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = fake_stdout
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""
        _, sha2 = pack_repo.pack(FIXTURE_DIR)
    assert sha == sha2


def test_pack_raises_when_repomix_missing() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError("repomix")):
        with pytest.raises(pack_repo.RepoMixNotFound):
            pack_repo.pack(FIXTURE_DIR)


def test_pack_raises_on_nonzero_exit() -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = ""
        mock_run.return_value.returncode = 2
        mock_run.return_value.stderr = "boom"
        with pytest.raises(pack_repo.RepoMixError) as exc:
            pack_repo.pack(FIXTURE_DIR)
        assert "boom" in str(exc.value)


def test_pack_target_must_be_directory(tmp_path: Path) -> None:
    f = tmp_path / "not_a_dir.txt"
    f.write_text("hi")
    with pytest.raises(NotADirectoryError):
        pack_repo.pack(f)


import shutil


@pytest.mark.live_repomix
@pytest.mark.skipif(
    shutil.which("repomix") is None,
    reason="repomix binary not on PATH",
)
def test_pack_live_against_tiny_repo() -> None:
    """Smoke against the real binary — only runs when repomix is on PATH."""
    text, sha = pack_repo.pack(FIXTURE_DIR)
    assert "main.py" in text
    assert len(sha) == 64
