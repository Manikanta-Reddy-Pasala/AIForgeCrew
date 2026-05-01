"""Runtime — ADK agent loop + 7 callbacks (no sandbox).

Callbacks ordered fixed (per spec §5.2):
  1 auditor.before
  2 circuit_breakers.check
  3 compactor.maybe_microcompact
  4 compactor.maybe_full_compact
  5 stuck_detector.check     (after_model)
  6 failure_taxonomy.match   (after_model + after_tool)
  7 auditor.after
  8 learner_hook.notify
"""
from __future__ import annotations
