"""Which build is this process running?

"Is my fix deployed?" could not be answered: nothing reported the commit. The
build is ``AIFORGE_BUILD_SHA`` when the image sets it, else the git HEAD of the
checkout the package was loaded from (read once), else "unknown".
"""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def build() -> dict:
    sha = os.environ.get("AIFORGE_BUILD_SHA", "").strip()
    when = os.environ.get("AIFORGE_BUILD_DATE", "").strip()
    if not sha:
        root = Path(__file__).resolve().parent.parent
        try:
            out = subprocess.run(
                ["git", "-C", str(root), "log", "-1", "--format=%h|%cI|%s"],
                capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                sha, when, subject = (out.stdout.strip().split("|", 2) + ["", ""])[:3]
                return {"commit": sha, "date": when, "subject": subject[:120]}
        except (OSError, subprocess.SubprocessError):
            pass
    return {"commit": sha or "unknown", "date": when, "subject": ""}
