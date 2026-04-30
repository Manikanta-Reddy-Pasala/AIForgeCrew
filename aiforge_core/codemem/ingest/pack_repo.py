"""Stage 1 — RepoMix pack.

Shells out to the `repomix` CLI (npm package) and captures stdout.
Returns (pack_text, sha256). Caller is responsible for hash-comparing
sha against state_db.merkle_repo to skip downstream stages.

Soft contract:
    - repomix binary missing → RepoMixNotFound (caller may fall back)
    - repomix nonzero exit   → RepoMixError(stderr) (caller logs + skips)

Defaults to: `repomix . --style markdown --output -` (stdout).
Override the binary via AIFORGE_CODEMEM_REPOMIX (e.g. "/opt/homebrew/bin/repomix").
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


class RepoMixNotFound(RuntimeError):
    pass


class RepoMixError(RuntimeError):
    pass


def _binary() -> str:
    return os.environ.get("AIFORGE_CODEMEM_REPOMIX", "repomix")


def pack(repo_path: str | Path) -> tuple[str, str]:
    """Run RepoMix on ``repo_path``; return (markdown_text, sha256_hex)."""
    repo_path = Path(repo_path).resolve()
    if not repo_path.is_dir():
        raise NotADirectoryError(f"{repo_path} is not a directory")

    try:
        proc = subprocess.run(
            [
                _binary(),
                str(repo_path),
                "--style", "markdown",
                "--output", "-",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError as exc:
        raise RepoMixNotFound(
            f"repomix binary not found (set AIFORGE_CODEMEM_REPOMIX or "
            f"`npm i -g repomix`): {exc}"
        ) from exc

    if proc.returncode != 0:
        raise RepoMixError(f"repomix exited {proc.returncode}: {proc.stderr.strip()}")

    text = proc.stdout
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, sha
