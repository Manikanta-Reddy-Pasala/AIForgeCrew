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
import pytest
import tempfile

os.environ.setdefault("AIFORGE_AUTODETECT_CTX", "0")

# The OKR scope classifier (md_store.classify_scope, wired into capture) makes
# one learner-role LLM call per captured fact. Off by default in the suite so
# captures don't attempt the network (fast, deterministic fallback = honour the
# caller's repo/topic hint); tests that exercise the LLM path set it to "1".
# Production leaves it unset → classify_scope's own default "1" → LLM on.
os.environ.setdefault("AIFORGE_OKR_SCOPE_LLM", "0")

# The operator's calls-per-minute ceiling (llm_max_rpm) is a real throttle: a
# suite that drives the transport more than the ceiling allows would sit in the
# limiter's queue for the rest of the run. Off in tests; the ceiling's own tests
# set it explicitly.
#
# NOT SUFFICIENT ON ITS OWN — see the _reset_llm_ceiling fixture below. A
# server-imposed HOLD applies even at rpm=0, lives in module state, and outlives
# the test that armed it by up to 60 real seconds.
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
#
# Assigned, NOT setdefault: `run.sh` exports AIFORGE_CONFIG_DIR=~/.aiforge, so
# any shell that had sourced it ran the whole suite against the operator's LIVE
# config — reading their real rules/memory (host-dependent results) and writing
# fixtures into it. The isolation has to be unconditional to be isolation.
# AIFORGE_TEST_CONFIG_DIR overrides it for the rare case that wants a fixed path.
os.environ["AIFORGE_CONFIG_DIR"] = (
    os.environ.get("AIFORGE_TEST_CONFIG_DIR")
    or tempfile.mkdtemp(prefix="aiforge-test-"))


# The suite is UNATTENDED by definition — no chat session, no approver — and
# the egress policy refuses outward writes in that state (see
# aiforge_core/net/egress.py). Almost every integration test drives a write
# verb to a fake server, so without this the policy would mask what those tests
# actually check. Declared once, here, rather than sprinkled through thirty
# files: tests/python/net/test_egress_policy.py clears it explicitly and is the
# one place the attendance rule itself is pinned.
os.environ.setdefault("AIFORGE_UNATTENDED_WRITES", "1")

# The egress allowlist DEFAULTS TO DENY and is seeded from the integrations the
# operator configured — a test box has none, so every fetch test would be
# refused before reaching the behaviour it is actually checking. Allow the
# RFC-2606 reserved domains (example.com/.org/.net and the .example TLD), which
# exist precisely for documentation and tests and can never resolve to anything
# real, plus x.io/ex.com which some older fixtures still use.
#
# Deliberately NOT a wildcard: a test that wants to prove the allowlist REFUSES
# something has to be able to name a host that is off it, and
# tests/python/config/test_egress_hosts.py + test_egress_policy.py override this
# to test the list itself.
os.environ.setdefault(
    "AIFORGE_EGRESS_ALLOW_HOSTS",
    "example.com,example.org,example.net,example,test,invalid,"
    "x.io,ex.com,x.example")


@pytest.fixture(autouse=True)
def _no_stale_egress_derivation():
    """The derived allowlist is memoized for a few seconds in production (it
    touches ~20 files and runs on every outbound decision). A test that sets
    CONFLUENCE_BASE_URL and immediately calls the tool would otherwise be judged
    against the PREVIOUS test's configuration — a whole-suite source of
    "passes alone, fails in a run"."""
    from aiforge_core.config import egress_hosts as _eh
    _eh._invalidate()
    yield
    _eh._invalidate()


@pytest.fixture(autouse=True)
def _reset_llm_ceiling():
    """Drop the rate ceiling's process-global state between tests.

    `AIFORGE_LLM_MAX_RPM=0` above turns off our own throttle but NOT a hold
    armed by a simulated server rejection: those apply at rpm=0 by design and
    persist in module state on the monotonic clock. Any test that drives a
    rate-limit body through the transport would otherwise add up to 60 seconds
    of real blocking to every later test in the run — a suite that hangs, with
    the cause several files away from the symptom.
    """
    from aiforge_core.llm import rate_limiter as _rl
    _rl.reset_global()
    yield
    _rl.reset_global()
