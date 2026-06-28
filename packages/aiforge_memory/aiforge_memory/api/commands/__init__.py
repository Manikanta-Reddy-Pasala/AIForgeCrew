"""Per-command modules for the ``aiforge-memory`` CLI.

Each subcommand exports a ``run(args)`` and a ``register(sub_parsers)``
hook; ``cli.py`` calls every module's ``register`` to compose the full
argparse tree. Adding a new command = drop a file + one line in
:data:`COMMAND_MODULES` below.
"""
from __future__ import annotations

from . import (
    decay,
    doctor,
    domains,
    forget,
    handoff,
    health,
    ingest,
    ingest_external,
    install_hook,
    link,
    list_memory,
    ops,
    recall,
    recap,
    remember,
    review,
    schedule,
    services,
    stats,
    summarise_symbols,
    tour,
    ui,
)
from . import (
    eval as eval_cmd,
)

# Order = order they appear in `aiforge-memory --help`. Keep grouped:
# core ingest/inspect first, then memory CRUD, then cross-repo, then ops.
COMMAND_MODULES = (
    ingest,
    ingest_external,
    stats,
    summarise_symbols,
    services,
    domains,
    tour,
    review,
    doctor,
    remember,
    recall,
    recap,
    forget,
    decay,
    handoff,
    list_memory,
    link,
    eval_cmd,
    install_hook,
    schedule,
    health,
    ops,
    ui,
)


__all__ = ["COMMAND_MODULES"]
