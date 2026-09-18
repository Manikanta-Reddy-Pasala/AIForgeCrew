"""Serving the web UI and quieting the access log: CORS origins, noisy poll
paths, and finding the built front end."""
from __future__ import annotations

import logging
import os


def _cors_origins() -> list[str]:
    """Allowlist from AIFORGE_CORS_ORIGINS (comma-separated); defaults to the
    localhost UI origins. NEVER ``*`` — this control plane mutates state."""
    raw = os.environ.get("AIFORGE_CORS_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return ["http://127.0.0.1:8799", "http://localhost:8799"]


# Quiet the uvicorn ACCESS log for high-frequency polls: the /admin page hits
# /api/admin/sync-status every 10s and probes hit /api/health, so each would
# otherwise write an access line several times a minute, forever, burying the
# lines that matter. This filters ONLY those paths (and only the access log —
# errors and app logs are untouched); override the set with
# AIFORGE_ACCESS_LOG_MUTE (comma-separated substrings), or "" to mute nothing.
class _MuteHighFrequencyPolls(logging.Filter):
    """Drop uvicorn.access lines whose path matches any muted substring."""

    def __init__(self, muted: list[str]):
        super().__init__()
        self._muted = muted

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access passes (client, method, full_path, http_ver, status)
        # as record.args; fall back to the formatted message otherwise.
        try:
            path = str(record.args[2]) if record.args else record.getMessage()
        except (IndexError, TypeError):
            path = record.getMessage()
        return not any(m in path for m in self._muted)


def _install_access_log_filter() -> None:
    raw = os.environ.get("AIFORGE_ACCESS_LOG_MUTE",
                         "/api/admin/sync-status,/api/health")
    muted = [s.strip() for s in raw.split(",") if s.strip()]
    if not muted:
        return
    log = logging.getLogger("uvicorn.access")
    # Match by CLASS NAME, not isinstance. A module reload (the test suite does
    # several, and uvicorn --reload does it in dev) rebinds this module's
    # `_MuteHighFrequencyPolls` to a BRAND NEW class object, so the filter
    # installed by the previous incarnation is not an instance of it — the
    # isinstance guard passed every time and stacked another filter on the
    # process-wide `uvicorn.access` logger. Reached 40 in one suite run.
    if not any(type(f).__name__ == "_MuteHighFrequencyPolls"
               for f in log.filters):
        log.addFilter(_MuteHighFrequencyPolls(muted))


# ─────────────────────────── Helpers ────────────────────────────────────
_INDEX_HTML = 'index.html'


# ─────────────────────────── Static UI ──────────────────────────────────
# If the Vite production build exists, serve it at /ui/ and redirect "/" to it.
# It lives where `npm run build` puts it: ../../web/dist. Every way AIForge
# runs today (run.sh, the docker sandbox, the `aiforge` binary's box) runs
# from a source tree, so that one place is enough.
def _resolve_dist() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "web", "dist"))


_DIST = _resolve_dist()
