"""Suite-wide test defaults.

C2: the /v1/models context-window auto-detect probe fires a GET (2-3s timeout
when no endpoint is up) on the pure-default config, thrashing the suite. Disable
it by default here so tests don't probe the network. Individual tests that WANT
to exercise the probe/enabled path override with ``monkeypatch.setenv`` (which
restores this default afterwards). ``setdefault`` means an explicit environment
value from the operator still wins.
"""
from __future__ import annotations

import os

os.environ.setdefault("AIFORGE_AUTODETECT_CTX", "0")

# The OKR scope classifier (md_store.classify_scope, wired into capture) makes
# one learner-role LLM call per captured fact. Off by default in the suite so
# captures don't attempt the network (fast, deterministic fallback = honour the
# caller's repo/topic hint); tests that exercise the LLM path set it to "1".
# Production leaves it unset → classify_scope's own default "1" → LLM on.
os.environ.setdefault("AIFORGE_OKR_SCOPE_LLM", "0")

# Recall map→summarize (recall_summary.summarize_hits) makes one learner-role
# LLM call when a query returns many hits. Off by default in the suite so recall
# paths don't attempt the network (callers fall back to the raw ranked list);
# tests that exercise the fold set it to "1". Production leaves it unset → on.
os.environ.setdefault("AIFORGE_UMEM_SUMMARIZE", "0")
