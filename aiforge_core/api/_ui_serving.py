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
#
# Two places, and the CHECKOUT one comes first:
#   1. A REPO CHECKOUT — ../../web/dist, where `npm run build` puts it.
#   2. INSTALLED (wheel / .deb / .app / .msi) — the build copies web/dist into
#      the package as aiforge_core/web_dist, because ../../web/dist from inside
#      site-packages is nowhere at all. Without this the packaged app serves a
#      working API and a 404 for its own UI.
#
# The order used to be the other way round, and it cost a real afternoon: once
# installer/build_payload.sh has run in a checkout, aiforge_core/web_dist stays
# behind as an untracked copy and SHADOWED the freshly built UI for ever after.
# The symptom is a new screen that never appears no matter how many times you
# rebuild — the API is new, the UI is frozen at whenever the payload was last
# built. An installed package has no ../../web/dist, so it is unaffected.
def _resolve_dist() -> str:
    here = os.path.dirname(__file__)
    candidates = (
        os.path.join(here, "..", "..", "web", "dist"),
        os.path.join(here, "..", "web_dist"),
    )
    return next((os.path.abspath(c) for c in candidates
                 if os.path.isdir(os.path.abspath(c))),
                os.path.abspath(candidates[0]))


_DIST = _resolve_dist()
