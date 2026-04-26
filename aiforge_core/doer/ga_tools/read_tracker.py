"""Read-tracker — wraps file_read with a per-handler cache + line numbers.

Mirrors Claude Code's Read tool. Two improvements over GA's bare
file_read:

1. **Line numbers in output** so quotes back from the model align
   exactly with the source (no off-by-one when the model claims
   'change line 42').
2. **Cache** — re-reading the same path returns the cached payload
   with a banner. Saves ~30-50% of doer turns spent re-reading
   files between edits.

Public surface: ``ReadTracker`` carries the per-run state; the GA
handler wraps ``do_file_read`` to call ``ReadTracker.read``.
"""
from __future__ import annotations

import os

# This tool reuses GA's existing 'file_read' tool name; it doesn't
# add a new tool to the schema. SCHEMA stays None so __init__ can
# import it but it's not advertised twice.
SCHEMA = None


class ReadTracker:
    """Per-run cache mapping abs_path -> (content, line_count).

    Stash on ``handler._aiforge_read_cache`` so subsequent file_read
    calls within the same Doer run hit the cache.
    """

    __slots__ = ("_cache",)

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, int]] = {}

    def is_cached(self, abs_path: str) -> bool:
        return abs_path in self._cache

    def read(self, abs_path: str, *, force: bool = False) -> str:
        """Read file, decorate with line numbers + cache hit banner."""
        if not force and abs_path in self._cache:
            content, _ = self._cache[abs_path]
            return self._with_banner(abs_path, content, cached=True)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except FileNotFoundError:
            return f"[file_read] {abs_path}: NOT FOUND"
        except Exception as exc:
            return f"[file_read] {abs_path}: {exc!r}"
        lines = content.splitlines()
        self._cache[abs_path] = (content, len(lines))
        return self._with_banner(abs_path, content, cached=False)

    @staticmethod
    def _with_banner(abs_path: str, content: str, *, cached: bool) -> str:
        rel = os.path.relpath(abs_path, os.getcwd())
        banner = (
            f"[file_read{' CACHED' if cached else ''}] "
            f"{rel} ({len(content.splitlines())} lines)"
        )
        if cached:
            banner += (" — already shown earlier this run; "
                       "do not request it again")
        # Line-numbered output, mirrors `cat -n`.
        numbered = "\n".join(
            f"{i + 1:6d}\t{line}"
            for i, line in enumerate(content.splitlines())
        )
        return banner + "\n" + numbered
