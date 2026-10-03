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
        # Only THIS checkout: without its own .git, `git -C` climbs to whatever
        # repository the package happens to be installed inside (a venv in some
        # project) and would report that project's last commit.
        if not (root / ".git").exists():
            return {"commit": "unknown", "date": when, "subject": ""}
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


def public() -> dict:
    """What an unauthenticated caller may see: the short commit and its date. The
    commit SUBJECT stays out — it often names the security fix it shipped."""
    b = build()
    return {"commit": b.get("commit"), "date": b.get("date")}
