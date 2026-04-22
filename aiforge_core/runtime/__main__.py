from __future__ import annotations

import sys

from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.graph_runner import run_graph


def main() -> int:
    ticket = tickets_mod.claim_next_any()
    if ticket is None:
        import logging
        logging.getLogger("aiforge.main").info("tick.idle — no todo tickets")
        return 0
    return run_graph(ticket.id)


if __name__ == "__main__":
    sys.exit(main())
