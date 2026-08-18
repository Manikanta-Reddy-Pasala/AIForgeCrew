"""Hub-and-spoke replication of the markdown memory tree.

Markdown is the source of truth and ``memory.db`` is a local derived index, so
syncing means replicating text files. One machine is the admin: spokes push what
they authored to it and pull back what it distilled, and no machine talks to any
other. See ``docs/superpowers/specs/2026-08-18-admin-memory-sync-design.md``.

No ``__all__`` and no re-exports: the package deliberately imports nothing at
import time, so ``from aiforge_core.memory.sync import loop`` costs only the one
submodule the caller named. An ``__all__`` listing submodules the package never
imports would only appear to work — it resolves when something else has already
imported them, and raises when nothing has.
"""
from __future__ import annotations
