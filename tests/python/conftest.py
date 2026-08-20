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
import tempfile

os.environ.setdefault("AIFORGE_AUTODETECT_CTX", "0")

# The OKR scope classifier (md_store.classify_scope, wired into capture) makes
# one learner-role LLM call per captured fact. Off by default in the suite so
# captures don't attempt the network (fast, deterministic fallback = honour the
# caller's repo/topic hint); tests that exercise the LLM path set it to "1".
# Production leaves it unset → classify_scope's own default "1" → LLM on.
os.environ.setdefault("AIFORGE_OKR_SCOPE_LLM", "0")

# The operator's calls-per-minute ceiling (llm_max_rpm, default 5) is a real
# throttle: a suite that drives the transport more than five times a minute
# would sit in the limiter's queue for the rest of the run. Off in tests; the
# ceiling's own tests set it explicitly.
os.environ.setdefault("AIFORGE_LLM_MAX_RPM", "0")

# Recall map→summarize (recall_summary.summarize_hits) makes one learner-role
# LLM call when a query returns many hits. Off by default in the suite so recall
# paths don't attempt the network (callers fall back to the raw ranked list);
# tests that exercise the fold set it to "1". Production leaves it unset → on.
os.environ.setdefault("AIFORGE_UMEM_SUMMARIZE", "0")

# Memory ISOLATION. Without this the suite reads and WRITES the operator's real
# ~/.aiforge: local runs left 101 fixture files in ~/.aiforge/memory (t1, t2,
# fresh-fact, five dated copies of one learning) and the eval runs minted junk
# topic briefs (hi-shout, isprime-function) in production memory, which then
# surfaced in recall.
#
# Only AIFORGE_CONFIG_DIR is redirected — memory_dir() derives
# ``<config>/memory`` from it, so pointing the root moves the md store, the OKF
# node tree and the SQLite db together. Setting AIFORGE_MEMORY_MD_DIR here
# instead would PIN the md store to one shared path and defeat the per-test
# fixtures that isolate by setting AIFORGE_CONFIG_DIR alone.
#
# A fresh dir per pytest process (not a fixed name) so two concurrent runs — and
# two successive ones — can't see each other's leftovers.
os.environ.setdefault("AIFORGE_CONFIG_DIR",
                      tempfile.mkdtemp(prefix="aiforge-test-"))
