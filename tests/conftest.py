"""Root test defaults — applies to every test directory, not just tests/python.

The operator's calls-per-minute ceiling (``llm_max_rpm``) is a real
throttle at the wire. Any suite that drives the transport more than five times
a minute would spend the rest of the run parked in the limiter's queue, so it
is off unless a test asks for it. ``setdefault`` keeps an explicit operator
value winning.
"""
from __future__ import annotations

import os

os.environ.setdefault("AIFORGE_LLM_MAX_RPM", "0")
# The log cleanup never runs on its own schedule in a test process: an API
# test boots the app, and an operator's exported AIFORGE_CONFIG_DIR would
# point it at the live logs.
os.environ.setdefault("AIFORGE_LOG_CLEANUP", "0")
os.environ.setdefault("AIFORGE_IDLE_COMPACT", "0")
# The goal check (runtime/chat_goals) is one more model call per message once a
# chat has a goal; a scripted agent would get it as its first call. Its own
# tests turn it on.
os.environ.setdefault("AIFORGE_CHAT_GOALS", "0")

# HERMETIC MODEL ENDPOINT for EVERY test directory (tests/python's conftest
# already did this for itself; tests/api had nothing). An unstubbed model call
# must fail fast against a port nothing listens on, never reach whatever the
# box runs on the default 127.0.0.1:1234 — on the nuc that is an SSH tunnel to
# a live model, and API tests then talked to it through the native
# tool-calling path, around the `llm.client.complete` stub they install, and
# failed only on that host. ASSIGNED, not setdefault: an operator's exported
# endpoint must not leak in either. Tests of endpoint resolution set their own.
_DEAD_ENDPOINT = "http://127.0.0.1:9/v1"
os.environ["AIFORGE_LM_BASE_URL"] = _DEAD_ENDPOINT
os.environ["AIFORGE_OPENAI_COMPAT_BASE_URL"] = _DEAD_ENDPOINT
# No probing a server for its window, and no waiting for a model that is down.
os.environ.setdefault("AIFORGE_AUTODETECT_CTX", "0")
os.environ.setdefault("AIFORGE_LLM_WAIT_MAX_S", "0.01")
# A chat step that keeps failing retries until the user presses Stop; nobody
# presses Stop in a test, so a failing step fails instead of hanging the run.
os.environ.setdefault("AIFORGE_CHAT_PERSIST_S", "-1")
os.environ.setdefault("AIFORGE_PIPELINE_PERSIST_S", "-1")
