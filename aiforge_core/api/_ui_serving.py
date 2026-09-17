"""Serving the web UI and quieting the access log: noisy poll paths, query
normalisation, and finding the built front end."""
from __future__ import annotations

import logging
import os
import re


def _pkg():
    """The package, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.api.api as package
    return package


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


_CHAT_SYSTEM = """You are the AIForge chat agent. The operator asks
questions about our OneShell codebase / past tickets / decisions. You
answer ONLY from the supplied ``## Context`` block — do NOT invent
file paths, symbols, versions, or commit shas the context doesn't
mention.

Output shape:
- 1-2 line direct answer up top.
- Then a short bullet list of the specific context rows you used
  (cite by [tier] and wing or ticket identifier).
- If the context is too thin to answer, say so in one line and
  suggest which MCP tool the operator should run (sym_lookup,
  cross_repo_flow, ticket_brief, etc.). No apology, no filler.
"""


_TICKET_RE = re.compile(r"\b(ONE-\d+)\b", re.I)
_CLASS_RE = re.compile(r"\b([A-Z][A-Za-z0-9]{3,})\b")
_REPO_RE = re.compile(r"\b(Pos[A-Z][A-Za-z]+|oneshell-[a-z-]+|MongoDbService|"
                      r"GatewayService|BusinessService|TallyConnector|"
                      r"EmailService|NotificationService|Gst[A-Z][A-Za-z]*|"
                      r"VendorIntegrationService|WhatsappApiService|"
                      r"Scheduler|QuartzScheduler|StoreIntelligence)\b")


_NORMALIZE_SYSTEM = """You are a query normalizer. The user will send one
short question that may contain typos, bad grammar, or missing articles.
Rewrite it as ONE clean English line that preserves intent, expands
obvious acronyms (pos → pos client backend, wg → wireguard), and fixes
typos. Do NOT answer the question. Do NOT add anything beyond the
rewritten query. Max 200 chars."""


def _normalize_query(query: str) -> str:
    """Tiny LLM pass that cleans typos + grammar so retrieval (BM25 and
    vector) actually hits. Falls back to the raw query on any failure.

    Skipped for queries already clean-ish (length < 12 chars, OR only
    one word) to avoid burning a call on trivial inputs.
    """
    q = query.strip()
    if len(q) < 12 or " " not in q:
        return q
    from aiforge_core.llm import complete as _complete
    try:
        result = _complete(
            "chat",
            [
                {"role": "system", "content": _NORMALIZE_SYSTEM},
                {"role": "user", "content": q[:600]},
            ],
            max_tokens=128, temperature=0.0,
            timeout_s=30,
        )
        if not result:
            return q
        # Strip stray quoting / leading labels.
        result = result.strip().strip('"\' ')
        for prefix in ("normalized:", "query:", "rewritten:"):
            if result.lower().startswith(prefix):
                result = result[len(prefix):].strip()
        return result[:300] or q
    except Exception:
        return q

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
