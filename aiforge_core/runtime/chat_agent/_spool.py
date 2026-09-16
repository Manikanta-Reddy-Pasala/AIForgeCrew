"""A shell command's output, written to temp files instead of pipes.

A pipe holds about 64 KB. A build that writes more blocks until someone reads
it, and ``run_command`` only read after the command exited — so a verbose
maven or pytest run hung until the timeout killed it. A file never fills up;
the size is capped instead (``AIFORGE_CHAT_CMD_OUTPUT_MAX_MB``, default 200,
checked every 0.2 s, so a very fast writer can overshoot it).
"""
from __future__ import annotations

import os
import tempfile

#: Characters of each stream kept for the model (the tail).
TAIL_CHARS = 80_000


def _max_bytes() -> int:
    try:
        mb = float(os.environ.get("AIFORGE_CHAT_CMD_OUTPUT_MAX_MB", "200"))
    except ValueError:
        mb = 200.0
    return int(mb * 1024 * 1024) if mb > 0 else 0


def _group_members(pgid: int) -> list[int]:
    members = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                fields = fh.read().rsplit(b")", 1)[1].split()
            if int(fields[2]) == pgid:        # state, ppid, pgrp
                members.append(int(name))
        except (OSError, IndexError, ValueError):
            continue
    return members


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
        # Progress bars redraw a line with a bare carriage return: keep what
        # the line finally showed.
        lines = text.replace("\r\n", "\n").split("\n")
        return "\n".join(line.rsplit("\r", 1)[-1] for line in lines)[-TAIL_CHARS:]

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
        proc_signals.stop_group(self.pgid, pause_s=0.2)

    def release_children(self) -> None:
        """After the command exits: a child still writing into this output (a
        bare `cmd &`) is stopped, as it was when the output went through a
        pipe. On Linux a child that redirected its own output keeps running;
        elsewhere every leftover child is stopped."""
        if self.pgid is None:
            return
        try:
            os.killpg(self.pgid, 0)
        except OSError:
            return                          # nothing left in the group
        if not os.path.isdir("/proc"):
            # No way to tell who writes where: stop the group, as the closed
            # pipe used to.
            self.kill_group()
        elif any(self._writes_here(pid) for pid in _group_members(self.pgid)):
            self.kill_group()

    def _writes_here(self, pid: int) -> bool:
        ours = set()
        for fh in (self.out, self.err):
            try:
                st = os.fstat(fh.fileno())
                ours.add((st.st_dev, st.st_ino))
            except (OSError, ValueError):
                pass
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            return False
        for fd in fds:
            try:
                st = os.stat(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if (st.st_dev, st.st_ino) in ours:
                return True
        return False

    def close(self) -> None:
        for fh in (self.out, self.err):
            try:
                fh.close()
            except OSError:
                pass
