"""The chat loop's logger and the constants its modules share."""
from __future__ import annotations

import logging

_log = logging.getLogger("aiforge.chat_agent")

_THE_FINALIZE_TOOL = 'the finalize tool'

# Cap on the per-turn signature tables (the action-strike table and the
# "files this turn has ever read" set). Both were bounded in practice by the
# 2000-step safety cap; an uncapped turn removes that ceiling.
_ACTION_SIG_MAX = 5000
