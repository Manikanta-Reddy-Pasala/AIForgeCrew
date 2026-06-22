"""Backwards-compatible shim — TLS context logic moved to ``aiforge_core.net.ssl``.

The SSL helper used to live here and only covered the three LLM call
sites. It now applies to all of AIForge's self-hosted outbound traffic
and lives in the shared :mod:`aiforge_core.net.ssl` module, host-scoped
so the verify opt-out can only relax internal hosts. This module
re-exports ``context_for`` so existing imports (``from .._ssl import
context_for``) keep working.
"""
from __future__ import annotations

from aiforge_core.net.ssl import context_for

__all__ = ["context_for"]
