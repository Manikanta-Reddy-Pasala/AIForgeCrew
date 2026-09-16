"""Save a running chat turn now and then, so a crash does not lose it.

A turn is written to the session only when it ends. A run that has worked for
three hours and then dies with the server (a restart, an out-of-memory kill, a
deploy) used to leave nothing: no steps, and no stopped turn for Retry to
resume. While a turn runs, its steps are written to a small file in the config
dir every ``AIFORGE_CHAT_SAVE_EVERY_S`` seconds (default 120). The file is
removed once the turn is saved normally. At startup, any file left behind
becomes the session's last assistant message, marked stopped, so Retry picks
the work up where it died.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from aiforge_core.config.paths import config_dir

log = logging.getLogger("aiforge.chat.turn_save")

INTERRUPTED_TEXT = ("(interrupted — the server stopped while this turn was "
                    "running; send the message again to continue from here)")


def _interval_s() -> float:
    try:
        return max(10.0, float(os.environ.get("AIFORGE_CHAT_SAVE_EVERY_S", "120")))
    except ValueError:
        return 120.0


def _dir() -> Path:
    return Path(os.path.expanduser(str(config_dir()))) / "running-turns"


def _path(session_id: int) -> Path:
    return _dir() / f"{int(session_id)}.json"


class TurnSaver:
    """Writes one session's running turn to disk at most every interval."""

    def __init__(self, session_id, mode: str = "simple") -> None:
        self.session_id = session_id
        self.mode = mode
        self._last = time.monotonic()
        self._saved_len = 0

    def maybe_save(self, steps: list, subtasks: list) -> None:
        if self.session_id is None or len(steps) == self._saved_len:
            return
        if time.monotonic() - self._last < _interval_s():
            return
        self.save(steps, subtasks)

    def save(self, steps: list, subtasks: list) -> None:
        self._last = time.monotonic()
        self._saved_len = len(steps)
        rows = ([{"type": "subtasks", "items": list(subtasks)}] if subtasks else []) \
            + list(steps)
        try:
            _dir().mkdir(parents=True, exist_ok=True)
            tmp = _path(self.session_id).with_suffix(".tmp")
            tmp.write_text(json.dumps({"mode": self.mode, "steps": rows},
                                      default=str), encoding="utf-8")
            os.replace(tmp, _path(self.session_id))
        except OSError as exc:
            log.warning("could not save running turn %s: %s", self.session_id, exc)

    def discard(self) -> None:
        if self.session_id is None:
            return
        try:
            _path(self.session_id).unlink(missing_ok=True)
        except OSError:
            pass


def recover_all() -> int:
    """Turn every file left by a crashed run into a stopped assistant message.
    Returns how many were recovered. Never raises."""
    from aiforge_core.runtime import chat_store
    recovered = 0
    try:
        files = sorted(_dir().glob("*.json"))
    except OSError:
        return 0
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            session_id = int(f.stem)
            if chat_store.get_session(session_id) is not None:
                steps = list(data.get("steps") or [])
                steps.append({"type": "stopped", "reason": "server_restart"})
                chat_store.add_message(session_id, "assistant", INTERRUPTED_TEXT,
                                       steps, mode=data.get("mode") or "simple")
                recovered += 1
        except Exception as exc:  # noqa: BLE001 — a bad file must not block boot
            log.warning("could not recover running turn %s: %s", f.name, exc)
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
    return recovered
