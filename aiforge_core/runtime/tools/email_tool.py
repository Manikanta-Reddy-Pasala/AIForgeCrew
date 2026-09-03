"""Email tool — send (SMTP) / read (IMAP) messages for the agent.

Mirrors the Jira/Confluence integration tools: config comes from env, every
function returns ``{"ok": bool, ...}`` and NEVER raises into the agent loop, and
an unconfigured integration soft-fails with a clear, actionable error.

This talks to the operator's OWN mail provider (SMTP/IMAP), a deliberate,
config-gated integration — it is NOT arbitrary web egress, so it does NOT go
through the SSRF web-fetch lockdown (``guard_public_url``). It is gated purely
on config presence (unset host ⇒ disabled) plus a master kill switch
(``AIFORGE_EMAIL_DISABLE=1``). Credentials are never logged.

Config resolves like Jira/GitLab: the env var WINS, else the value persisted
from the Settings → Integrations "Email" panel (stored under key ``email`` in
``integrations.json``). This keeps offline/env-only deployments working while
letting the UI drive config with no secrets in the repo.

Config (env  ·  UI-store key):
  SMTP (send):
    AIFORGE_SMTP_HOST      · smtp_host      mail submission host (unset ⇒ send off)
    AIFORGE_SMTP_PORT      · smtp_port      default 587
    AIFORGE_SMTP_USER      · smtp_user      login user
    AIFORGE_SMTP_PASSWORD  · smtp_password  login password / app token
    AIFORGE_SMTP_FROM      · smtp_from      From address (default = SMTP_USER)
    AIFORGE_SMTP_STARTTLS  · smtp_starttls  "1" (default) STARTTLS · "0" ⇒ implicit SSL
  IMAP (read):
    AIFORGE_IMAP_HOST      · imap_host      mailbox host (unset ⇒ read off)
    AIFORGE_IMAP_PORT      · imap_port      default 993
    AIFORGE_IMAP_USER      · imap_user      login user
    AIFORGE_IMAP_PASSWORD  · imap_password  login password / app token
    AIFORGE_IMAP_SSL       · imap_ssl       "1" (default) ⇒ IMAP4_SSL · "0" ⇒ plain
  AIFORGE_EMAIL_DISABLE=1    master kill switch — both tools report disabled
"""
from __future__ import annotations

import email as _email
import contextlib
import imaplib
import os
import smtplib
from email.message import EmailMessage

from . import _http_integration as _http

_EMAIL_DISABLED_AIFORGE_EMAIL = 'email disabled (AIFORGE_EMAIL_DISABLE=1)'

_TIMEOUT_S = 30
_SNIPPET_CAP = 500

_truthy = _http.truthy


# ─────────────────────────── config ─────────────────────────────────

def _disabled() -> bool:
    return _truthy(os.environ.get("AIFORGE_EMAIL_DISABLE", ""))


def _stored() -> dict:
    """UI-persisted email settings (empty when the store/entry is absent)."""
    try:
        from aiforge_core.config import integrations
        return integrations.get("email")
    except Exception:  # noqa: BLE001
        return {}


def _str_conf(env_name: str, stored_val, default: str = "") -> str:
    """Env var WINS, else the stored value, else ``default``."""
    v = (os.environ.get(env_name) or "").strip()
    if v:
        return v
    if stored_val is not None and str(stored_val).strip():
        return str(stored_val).strip()
    return default


def _secret_conf(env_name: str, stored_val) -> str:
    """Password: env WINS (raw, unstripped), else stored value."""
    v = os.environ.get(env_name)
    if v:
        return v
    return stored_val if isinstance(stored_val, str) else ""


def _int_conf(env_name: str, stored_val, default: int) -> int:
    raw = (os.environ.get(env_name) or "").strip()
    if not raw and stored_val is not None:
        raw = str(stored_val).strip()
    try:
        return int(raw or default)
    except (ValueError, TypeError):
        return default


def _bool_conf(env_name: str, stored_val, default: bool) -> bool:
    """Env WINS when set (even to "0"), else the stored bool, else ``default``."""
    raw = os.environ.get(env_name)
    if raw is not None and raw != "":
        return _truthy(raw)
    if stored_val is not None:
        return bool(stored_val)
    return default


def _smtp_conf() -> dict:
    s = _stored()
    user = _str_conf("AIFORGE_SMTP_USER", s.get("smtp_user"))
    return {
        "host": _str_conf("AIFORGE_SMTP_HOST", s.get("smtp_host")),
        "port": _int_conf("AIFORGE_SMTP_PORT", s.get("smtp_port"), 587),
        "user": user,
        "password": _secret_conf("AIFORGE_SMTP_PASSWORD", s.get("smtp_password")),
        "from": _str_conf("AIFORGE_SMTP_FROM", s.get("smtp_from")) or user,
        "starttls": _bool_conf("AIFORGE_SMTP_STARTTLS", s.get("smtp_starttls"), True),
    }


def _imap_conf() -> dict:
    s = _stored()
    return {
        "host": _str_conf("AIFORGE_IMAP_HOST", s.get("imap_host")),
        "port": _int_conf("AIFORGE_IMAP_PORT", s.get("imap_port"), 993),
        "user": _str_conf("AIFORGE_IMAP_USER", s.get("imap_user")),
        "password": _secret_conf("AIFORGE_IMAP_PASSWORD", s.get("imap_password")),
        "ssl": _bool_conf("AIFORGE_IMAP_SSL", s.get("imap_ssl"), True),
    }


def _as_list(v) -> list[str]:
    """Coerce a str/list address field into a clean list of addresses."""
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v).split(",") if s.strip()]


# ─────────────────────────── send (SMTP) ────────────────────────────

def _build_message(c: dict, args: dict, to: list, cc: list) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = c["from"] or c["user"]
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = str(args.get("subject") or "")
    msg.set_content(str(args.get("body") or ""))
    if args.get("html"):
        msg.add_alternative(str(args["html"]), subtype="html")
    return msg


def _smtp_open(c: dict):
    """A connected, authenticated SMTP session (STARTTLS or implicit SSL)."""
    if c["starttls"]:
        srv = smtplib.SMTP(c["host"], c["port"], timeout=_TIMEOUT_S)
        srv.ehlo()
        srv.starttls()
        srv.ehlo()
    else:
        srv = smtplib.SMTP_SSL(c["host"], c["port"], timeout=_TIMEOUT_S)
    if c["user"]:
        srv.login(c["user"], c["password"])
    return srv


def email_send(args: dict, _cwd: str | None = None) -> dict:
    """Send an email via SMTP. ``args``: ``to`` (str|list, required),
    ``subject``, ``body`` (plain text); optional ``html``, ``cc``, ``bcc``."""
    if _disabled():
        return {"ok": False, "error": _EMAIL_DISABLED_AIFORGE_EMAIL}
    c = _smtp_conf()
    if not c["host"]:
        return {"ok": False,
                "error": "email not configured (set AIFORGE_SMTP_HOST/USER/PASSWORD)"}
    # Mail is the one channel that reaches a person who never asked for it, so
    # it is its own egress class AND always a write: an unattended run cannot
    # send unless the operator opted in.
    from aiforge_core.net import egress as _egress
    refusal = _egress.allow("email", c["host"], method="POST")
    if refusal is not None:
        return refusal
    to = _as_list(args.get("to"))
    if not to:
        return {"ok": False, "error": "missing 'to'"}
    cc, bcc = _as_list(args.get("cc")), _as_list(args.get("bcc"))
    try:
        msg = _build_message(c, args, to, cc)
        srv = _smtp_open(c)
        try:
            # bcc kept out of the headers, still delivered
            srv.send_message(msg, to_addrs=to + cc + bcc)
        finally:
            with contextlib.suppress(Exception):
                srv.quit()
        return {"ok": True, "to": to, "cc": cc,
                "subject": str(args.get("subject") or "")}
    except Exception as exc:  # noqa: BLE001 — never raise into the agent loop
        return {"ok": False, "error": str(exc)}


# ─────────────────────────── read (IMAP) ────────────────────────────

def _decode_header(val) -> str:
    if not val:
        return ""
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(str(val))))
    except Exception:  # noqa: BLE001
        return str(val)


def _to_text(payload: bytes, part) -> str:
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, AttributeError):
        return payload.decode("utf-8", errors="replace")


def _is_readable_text_part(part) -> bool:
    """A plain-text body part — not a multipart wrapper, not an attachment."""
    if part.get_content_maintype() == "multipart":
        return False
    if "attachment" in str(part.get("Content-Disposition") or "").lower():
        return False
    return part.get_content_type() == "text/plain"


def _multipart_snippet(m) -> str:
    for part in m.walk():
        payload = (part.get_payload(decode=True)
                   if _is_readable_text_part(part) else None)
        if payload:
            return _to_text(payload, part).strip()[:_SNIPPET_CAP]
    return ""


def _single_part_snippet(m) -> str:
    payload = m.get_payload(decode=True)
    if payload:
        return _to_text(payload, m).strip()[:_SNIPPET_CAP]
    return str(m.get_payload() or "")[:_SNIPPET_CAP]


def _body_snippet(m) -> str:
    """First ~500 chars of the plain-text body; attachments soft-skipped."""
    try:
        return (_multipart_snippet(m) if m.is_multipart()
                else _single_part_snippet(m))
    except Exception:  # noqa: BLE001
        return ""


def _first_bytes(msg_data) -> bytes:
    for part in msg_data or []:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(
                part[1], (bytes, bytearray)):
            return bytes(part[1])
    return b""


def _imap_connect(c: dict):
    # Egress: reading mail is an outbound connection with a credential on it,
    # and it was the one email path the policy never saw — AIFORGE_EGRESS_OFF
    # closed send() and left read/search pulling message bodies into context.
    from aiforge_core.net import egress as _egress
    _ref = _egress.allow("email", str(c.get("host") or ""))
    if _ref is not None:
        raise OSError(f"egress refused IMAP: {_ref.get('error')}")
    return (imaplib.IMAP4_SSL(c["host"], c["port"]) if c["ssl"]
            else imaplib.IMAP4(c["host"], c["port"]))


def _read_limit(args: dict) -> int:
    try:
        return max(1, int(args.get("limit", 10) or 10))
    except (TypeError, ValueError):
        return 10


def _message_row(num, m) -> dict:
    return {"uid": num.decode() if isinstance(num, bytes) else str(num),
            "from": _decode_header(m.get("From", "")),
            "to": _decode_header(m.get("To", "")),
            "subject": _decode_header(m.get("Subject", "")),
            "date": m.get("Date", ""),
            "snippet": _body_snippet(m)}


def _fetch_message(srv, num):
    """The parsed message, or None when it cannot be read."""
    try:
        _typ, msg_data = srv.fetch(num, "(RFC822)")
    except Exception:  # noqa: BLE001 — skip an unreadable message
        return None
    raw = _first_bytes(msg_data)
    return _email.message_from_bytes(raw) if raw else None


def _collect_messages(srv, folder: str, *, unseen: bool, query: str,
                      limit: int) -> list[dict]:
    """Newest-first messages matching ``query`` (subject + from)."""
    srv.select(folder)
    _typ, data = srv.search(None, *(["UNSEEN"] if unseen else ["ALL"]))
    ids = list(reversed(data[0].split() if data and data[0] else []))
    out: list[dict] = []
    for num in ids:
        if len(out) >= limit:
            break
        m = _fetch_message(srv, num)
        if m is None:
            continue
        row = _message_row(num, m)
        haystack = row["subject"].lower() + " " + row["from"].lower()
        if query and query not in haystack:
            continue
        out.append(row)
    return out


def email_read(args: dict, _cwd: str | None = None) -> dict:
    """Fetch recent/matching emails via IMAP. ``args``: optional ``folder``
    (default INBOX), ``limit`` (default 10), ``query``/``search`` (substring
    matched against subject+from), ``unseen_only`` (bool). Newest first."""
    if _disabled():
        return {"ok": False, "error": _EMAIL_DISABLED_AIFORGE_EMAIL}
    c = _imap_conf()
    if not c["host"]:
        return {"ok": False,
                "error": "email not configured (set AIFORGE_IMAP_HOST/USER/PASSWORD)"}
    try:
        srv = _imap_connect(c)
        try:
            if c["user"]:
                srv.login(c["user"], c["password"])
            messages = _collect_messages(
                srv, str(args.get("folder") or "INBOX"),
                unseen=_truthy(args.get("unseen_only")),
                query=str(args.get("query") or args.get("search") or "")
                .strip().lower(),
                limit=_read_limit(args))
            with contextlib.suppress(Exception):
                srv.close()
            return {"ok": True, "messages": messages}
        finally:
            with contextlib.suppress(Exception):
                srv.logout()
    except Exception as exc:  # noqa: BLE001 — never raise into the agent loop
        return {"ok": False, "error": str(exc)}


# email_search is a thin alias — email_read already accepts ``query``.
def email_search(args: dict, cwd: str | None = None) -> dict:
    """Alias of :func:`email_read` (kept for a natural verb the model may emit)."""
    return email_read(args, cwd)


# ─────────────────────────── connectivity test ──────────────────────

def _smtp_probe(c: dict) -> None:
    """Connect (and log in, when a user is set). Raises on failure."""
    if c["starttls"]:
        srv = smtplib.SMTP(c["host"], c["port"], timeout=_TIMEOUT_S)
        try:
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
            if c["user"]:
                srv.login(c["user"], c["password"])
        finally:
            with contextlib.suppress(Exception):
                srv.quit()
        return
    srv = smtplib.SMTP_SSL(c["host"], c["port"], timeout=_TIMEOUT_S)
    try:
        if c["user"]:
            srv.login(c["user"], c["password"])
    finally:
        with contextlib.suppress(Exception):
            srv.quit()


def _imap_probe(c: dict) -> None:
    srv = _imap_connect(c)
    try:
        if c["user"]:
            srv.login(c["user"], c["password"])
    finally:
        with contextlib.suppress(Exception):
            srv.logout()


def _probe_side(c: dict, probe) -> dict | None:
    """``{ok, host, …}`` for a configured side; None when it isn't configured."""
    if not c["host"]:
        return None
    try:
        probe(c)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "host": c["host"], "error": str(exc)}
    return {"ok": True, "host": c["host"], "port": c["port"], "user": c["user"]}


def email_test() -> dict:
    """Connectivity + auth check for the Settings UI. Connects (and logs in,
    when a user is set) to whichever of SMTP/IMAP is configured. Returns
    ``{"ok": bool, "smtp": {...}, "imap": {...}}`` — ``ok`` is true when every
    *configured* side connected. Never raises."""
    if _disabled():
        return {"ok": False, "error": _EMAIL_DISABLED_AIFORGE_EMAIL}
    smtp_c, imap_c = _smtp_conf(), _imap_conf()
    if not smtp_c["host"] and not imap_c["host"]:
        return {"ok": False, "error": "email_not_configured",
                "hint": "set an SMTP host (send) and/or an IMAP host (read)"}
    sides = {"smtp": _probe_side(smtp_c, _smtp_probe),
             "imap": _probe_side(imap_c, _imap_probe)}
    errors = [v["error"] for v in sides.values()
              if isinstance(v, dict) and v.get("error")]
    result: dict = {"ok": not errors, **sides}
    if errors:
        result["error"] = "; ".join(errors)
    return result


__all__ = ["email_send", "email_read", "email_search", "email_test"]
