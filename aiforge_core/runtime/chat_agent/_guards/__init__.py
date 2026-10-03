"""Response guards: checks on a FINAL answer against what really happened.

``base`` holds the shared nudge-then-disclaim loop; each other module is one
guard (``file_edit``, ``external``, ``zero_edit``, ``turn_facts``) or the
action-log sanitiser (``echo``). ``_turn/_finish.py`` decides which run and in what order.
"""
from __future__ import annotations

from .base import ClaimGuard, run_guards
from .echo import EchoGuard
from .external import ExternalClaimGuard
from .file_edit import FileEditClaimGuard
from .turn_facts import UnchangedFileClaimGuard, UnpassedCheckGuard
from .zero_edit import ZeroEditGuard

__all__ = ["ClaimGuard", "run_guards", "EchoGuard", "ExternalClaimGuard",
           "FileEditClaimGuard", "ZeroEditGuard", "UnchangedFileClaimGuard",
           "UnpassedCheckGuard"]
