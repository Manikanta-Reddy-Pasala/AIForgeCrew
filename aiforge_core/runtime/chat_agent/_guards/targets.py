"""FINAL guard: the answer asks the user to paste a target again although the
target is on disk. Sends the model back once with the path; then labels."""
from __future__ import annotations

from aiforge_core.runtime import targets


class TargetReaskGuard:
    counter = "target_reask_nudges"
    strip_body = False
    echo_text = False

    def __init__(self) -> None:
        self._rows: list = []

    def applies(self, st) -> bool:
        # ``targets.list`` is [] when targets are off (AIFORGE_CHAT_TARGETS=0),
        # the session is unknown or nothing was captured — the cheap no-op.
        self._rows = targets.list(getattr(st, "session_id", None))
        return bool(self._rows)

    def budget(self, st) -> int:
        return 1

    def detect(self, text: str) -> list:
        return ["reask"] if targets.asks_for_target(text) else []

    def evidence(self, st):
        return False

    def notice(self, claims) -> str:
        return "⚠ the answer asks for the target again — it is on disk; sending the agent back"

    def _paths(self) -> str:
        return ", ".join(f"{r['id']} → {r['path']}" for r in self._rows)

    def nudge(self, claims) -> str:
        return ("The target data is already on disk; do not ask the user for it. "
                f"Read it with file_read: {self._paths()}. Then continue the task.")

    def disclaimer(self, claims) -> str:
        return ("⚠ **Harness target check:** the target was given earlier and is on disk "
                f"({self._paths()}); the agent asked for it again.\n\n")


__all__ = ["TargetReaskGuard"]
