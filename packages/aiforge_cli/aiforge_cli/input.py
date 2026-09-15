"""Asking for a line, and completing what is being typed.

The completer is the in-chat half of commands.py: the same table that produces
`/help` and the shell scripts decides what TAB offers, and the live lists
(models, sessions) come from callbacks so this module never talks to the API
itself.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from . import commands as tbl


class _Cache:
    """A short-lived cache for the live completion lists.

    TAB must not wait on an HTTP round trip on every keystroke, and a model
    list does not change between two presses.
    """

    def __init__(self, ttl: float = 60.0):
        self.ttl = ttl
        self._data: dict[str, tuple[float, list[tuple[str, str]]]] = {}

    def get(self, key: str, produce: Callable[[], list[tuple[str, str]]]) -> list[tuple[str, str]]:
        now = time.monotonic()
        hit = self._data.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        try:
            value = produce()
        except Exception:  # noqa: BLE001 — completion is never worth an error
            value = hit[1] if hit else []
        self._data[key] = (now, value)
        return value


class ChatCompleter(Completer):
    def __init__(self, *, models: Callable[[], list[tuple[str, str]]],
                 sessions: Callable[[], list[tuple[str, str]]]):
        self._models = models
        self._sessions = sessions
        self._cache = _Cache()
        self._paths = PathCompleter(expanduser=True)
        self._dirs = PathCompleter(only_directories=True, expanduser=True)

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        # @file — a path the user wants the agent to read, completed in place.
        at = text.rfind("@")
        if at != -1 and (at == 0 or text[at - 1].isspace()) and "/" not in text[:at]:
            yield from self._delegate(self._paths, document, text[at + 1:], complete_event)
            return
        if not text.startswith("/"):
            return
        parts = text.split()
        if len(parts) <= 1 and not text.endswith(" "):
            word = parts[0] if parts else "/"
            for cmd in tbl.SLASH:
                if cmd.name.startswith(word):
                    yield Completion(cmd.name, start_position=-len(word),
                                     display=cmd.name, display_meta=cmd.help)
            return
        cmd = tbl.by_name(parts[0], tbl.SLASH)
        if cmd is None:
            return
        word = "" if text.endswith(" ") else parts[-1]
        for value, meta in self._values(cmd, parts):
            if value.startswith(word):
                yield Completion(value, start_position=-len(word), display_meta=meta)
        if cmd.arg in (tbl.ARG_DIR, tbl.ARG_FILE):
            sub = self._dirs if cmd.arg == tbl.ARG_DIR else self._paths
            yield from self._delegate(sub, document, word, complete_event)

    def _values(self, cmd: tbl.Command, parts: list[str]) -> list[tuple[str, str]]:
        # `/mount add <dir>` — the choices belong to the second word only.
        if cmd.choices and len(parts) <= 2:
            return [(c, "") for c in cmd.choices]
        if cmd.arg == tbl.ARG_MODEL:
            return self._cache.get("models", self._models)
        if cmd.arg == tbl.ARG_SESSION:
            return self._cache.get("sessions", self._sessions)
        if cmd.arg == tbl.ARG_COMMAND:
            return [(c.name, c.help) for c in tbl.SLASH]
        return []

    @staticmethod
    def _delegate(sub: PathCompleter, document, word: str, complete_event):
        from prompt_toolkit.document import Document
        for completion in sub.get_completions(Document(word, len(word)), complete_event):
            yield completion


def build_session(history_file: Path, completer: Completer) -> PromptSession:
    """The line editor: history, TAB, and Alt+Enter for a newline."""
    bindings = KeyBindings()

    @bindings.add("escape", "enter")
    def _newline(event) -> None:
        event.current_buffer.insert_text("\n")

    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(history_file))
    except OSError:
        history = None
    return PromptSession(history=history, completer=completer, key_bindings=bindings,
                         complete_while_typing=False,
                         enable_history_search=True, multiline=False)


def prompt_dirs(word: str) -> list[str]:
    """Directory names under ``word`` — used by the non-interactive paths."""
    base = os.path.expanduser(word) or "."
    parent = base if base.endswith(os.sep) else os.path.dirname(base) or "."
    try:
        return sorted(p.name for p in Path(parent).iterdir() if p.is_dir())
    except OSError:
        return []
