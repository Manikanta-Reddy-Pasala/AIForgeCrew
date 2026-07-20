"""Peer-to-peer replication of the markdown memory tree.

Markdown is the source of truth and ``memory.db`` is a local derived index, so
syncing means replicating text files. See
``docs/superpowers/specs/2026-07-19-p2p-shared-memory-design.md``.

No ``__all__`` and no re-exports: the package deliberately imports nothing at
import time, so ``from aiforge_core.memory.sync import loop`` costs only the one
submodule the caller named. An ``__all__`` listing submodules the package never
imports would only appear to work — it resolves when something else has already
imported them, and raises when nothing has.
"""
from __future__ import annotations
