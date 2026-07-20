"""The uvicorn access log must not spam the high-frequency polls.

The /admin page hits /api/admin/sync-status every 10s and probes hit
/api/health, so without a filter each writes an access line several times a
minute forever, burying the lines that matter.
"""
from __future__ import annotations

import logging

import aiforge_core.api.api as api


def _rec(path):
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", path, "1.1", 200), None)


def _kept(path):
    log = logging.getLogger("uvicorn.access")
    return all(f.filter(_rec(path)) for f in log.filters)


def test_polling_paths_are_muted():
    api._install_access_log_filter()          # idempotent
    assert _kept("/api/admin/sync-status") is False
    assert _kept("/api/health") is False


def test_real_requests_still_log():
    api._install_access_log_filter()
    assert _kept("/api/chat/sessions/1/message") is True
    assert _kept("/api/memory/sync/manifest") is True


def test_filter_is_idempotent():
    log = logging.getLogger("uvicorn.access")
    before = len(log.filters)
    api._install_access_log_filter()
    api._install_access_log_filter()
    # No duplicate filters stacked on repeated import/boot.
    muters = [f for f in log.filters if type(f).__name__ == "_MuteHighFrequencyPolls"]
    assert len(muters) == 1
    assert len(log.filters) >= before
