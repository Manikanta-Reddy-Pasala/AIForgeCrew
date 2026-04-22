from __future__ import annotations

import sys

from .feature_flags import get_flag
from .orchestrator import _cli


def _graph_entry() -> int:
    from aiforge_core.runtime import tickets as tickets_mod
    from aiforge_core.runtime.graph_runner import run_graph

    ticket = tickets_mod.claim_next_any()
    if ticket is None:
        return 0
    return run_graph(ticket.id)


if __name__ == "__main__":
    if get_flag("orchestrator.backend", "legacy") == "langgraph":
        sys.exit(_graph_entry())
    else:
        _cli()
