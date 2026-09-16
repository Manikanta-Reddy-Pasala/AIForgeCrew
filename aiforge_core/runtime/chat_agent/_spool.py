"""A shell command's output, written to temp files instead of pipes.

A pipe holds about 64 KB. A build that writes more blocks until someone reads
it, and ``run_command`` only read after the command exited — so a verbose
maven or pytest run hung until the timeout killed it. A file never fills up;
the size is capped instead (``AIFORGE_CHAT_CMD_OUTPUT_MAX_MB``, default 200)
so a runaway command cannot fill the disk.
"""
from __future__ import annotations

import os
import signal
import tempfile

#: Characters of each stream kept for the model (the tail).
TAIL_CHARS = 80_000


def _max_bytes() -> int:
    try:
        mb = float(os.environ.get("AIFORGE_CHAT_CMD_OUTPUT_MAX_MB", "200"))
    except ValueError:
        mb = 200.0
    return int(mb * 1024 * 1024) if mb > 0 else 0


def proc_group(proc) -> int | None:
    from aiforge_core.runtime import proc_signals
    return proc_signals.group_of(proc)


class Spool:
    def __init__(self) -> None:
        self.out = tempfile.TemporaryFile()
        self.err = tempfile.TemporaryFile()
        self.pgid: int | None = None

    @staticmethod
    def _tail(fh) -> str:
        fh.flush()
        end = fh.seek(0, os.SEEK_END)
        fh.seek(max(0, end - TAIL_CHARS * 4))
        text = fh.read().decode("utf-8", "replace")
        # Progress bars redraw with a bare carriage return.
        return text.replace("\r\n", "\n").replace("\r", "\n")[-TAIL_CHARS:]

    def read(self) -> tuple[str, str]:
        return self._tail(self.out), self._tail(self.err)

    def size(self) -> int:
        total = 0
        for fh in (self.out, self.err):
            try:
                total += os.fstat(fh.fileno()).st_size
            except (OSError, ValueError):
                pass
        return total

    def too_big(self) -> bool:
        cap = _max_bytes()
        return bool(cap) and self.size() > cap

    @staticmethod
    def too_big_error() -> str:
        return (f"stopped: the command wrote more than "
                f"{_max_bytes() // (1024 * 1024)} MB of output. Re-run it with "
                "less output (a quieter flag, a filter, or redirect to a file "
                "and read the part you need).")

    def kill_group(self) -> None:
        """Stop whatever is left of the command's process group."""
        if self.pgid is None:
            return
        from aiforge_core.runtime import proc_signals
        proc_signals.kill_group(self.pgid, signal.SIGKILL)

    def close(self) -> None:
        for fh in (self.out, self.err):
            try:
                fh.close()
            except OSError:
                pass
