"""Email tool — send (SMTP) / read (IMAP) messages for the agent.

Mirrors the Jira/Confluence integration tools: config comes from env, every
function returns ``{"ok": bool, ...}`` and NEVER raises into the agent loop, and
an unconfigured integration soft-fails with a clear, actionable error.

This talks to the operator's OWN mail provider (SMTP/IMAP), a deliberate,
config-gated integration — it is NOT arbitrary web egress, so it does NOT go
through the SSRF web-fetch lockdown (``guard_public_url``). It is gated purely
on config presence (unset host ⇒ disabled) plus a master kill switch
(``AIFORGE_EMAIL_DISABLE=1``). Credentials are read from env ONLY and are never
logged.

Config (env):
  SMTP (send):
    AIFORGE_SMTP_HOST        mail submission host (unset ⇒ send disabled)
    AIFORGE_SMTP_PORT        default 587
    AIFORGE_SMTP_USER        login user
    AIFORGE_SMTP_PASSWORD    login password / app token
    AIFORGE_SMTP_FROM        From address (default = SMTP_USER)
    AIFORGE_SMTP_STARTTLS    "1" (default) STARTTLS on 587 · "0" ⇒ implicit SSL
  IMAP (read):
    AIFORGE_IMAP_HOST        mailbox host (unset ⇒ read disabled)
    AIFORGE_IMAP_PORT        default 993
    AIFORGE_IMAP_USER        login user
    AIFORGE_IMAP_PASSWORD    login password / app token
    AIFORGE_IMAP_SSL         "1" (default) ⇒ IMAP4_SSL · "0" ⇒ plain IMAP4
  AIFORGE_EMAIL_DISABLE=1    master kill switch — both tools report disabled
"""
from __future__ import annotations

import email as _email
import imaplib
import os
import smtplib
from email.message import EmailMessage

from . import _http_integration as _http

_TIMEOUT_S = 30
_SNIPPET_CAP = 500

_truthy = _http.truthy


# ─────────────────────────── config ─────────────────────────────────

def _disabled() -> bool:
    return _truthy(os.environ.get("AIFORGE_EMAIL_DISABLE", ""))


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def _smtp_conf() -> dict:
    user = (os.environ.get("AIFORGE_SMTP_USER") or "").strip()
    return {
        "host": (os.environ.get("AIFORGE_SMTP_HOST") or "").strip(),
        "port": _int_env("AIFORGE_SMTP_PORT", 587),
        "user": user,
        "password": os.environ.get("AIFORGE_SMTP_PASSWORD") or "",
        "from": ((os.environ.get("AIFORGE_SMTP_FROM") or "").strip() or user),
        "starttls": _truthy(os.environ.get("AIFORGE_SMTP_STARTTLS", "1")),
    }


def _imap_conf() -> dict:
    return {
        "host": (os.environ.get("AIFORGE_IMAP_HOST") or "").strip(),
        "port": _int_env("AIFORGE_IMAP_PORT", 993),
        "user": (os.environ.get("AIFORGE_IMAP_USER") or "").strip(),
        "password": os.environ.get("AIFORGE_IMAP_PASSWORD") or "",
        "ssl": _truthy(os.environ.get("AIFORGE_IMAP_SSL", "1")),
    }


def _as_list(v) -> list[str]:
    """Coerce a str/list address field into a clean list of addresses."""
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v).split(",") if s.strip()]


# ─────────────────────────── send (SMTP) ────────────────────────────

def email_send(args: dict, cwd: str | None = None) -> dict:
    """Send an email via SMTP. ``args``: ``to`` (str|list, required),
    ``subject``, ``body`` (plain text); optional ``html``, ``cc``, ``bcc``."""
    if _disabled():
        return {"ok": False, "error": "email disabled (AIFORGE_EMAIL_DISABLE=1)"}
    c = _smtp_conf()
    if not c["host"]:
        return {"ok": False,
                "error": "email not configured (set AIFORGE_SMTP_HOST/USER/PASSWORD)"}
    to = _as_list(args.get("to"))
    if not to:
        return {"ok": False, "error": "missing 'to'"}
    cc = _as_list(args.get("cc"))
    bcc = _as_list(args.get("bcc"))
    subject = str(args.get("subject") or "")
    body = str(args.get("body") or "")
    try:
        msg = EmailMessage()
        msg["From"] = c["from"] or c["user"]
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg.set_content(body)
        if args.get("html"):
            msg.add_alternative(str(args["html"]), subtype="html")
        recipients = to + cc + bcc  # bcc kept out of headers, still delivered
        if c["starttls"]:
            srv = smtplib.SMTP(c["host"], c["port"], timeout=_TIMEOUT_S)
            try:
                srv.ehlo()
                srv.starttls()
                srv.ehlo()
                if c["user"]:
                    srv.login(c["user"], c["password"])
                srv.send_message(msg, to_addrs=recipients)
            finally:
                try:
                    srv.quit()
                except Exception:  # noqa: BLE001
                    pass
        else:
            srv = smtplib.SMTP_SSL(c["host"], c["port"], timeout=_TIMEOUT_S)
            try:
                if c["user"]:
                    srv.login(c["user"], c["password"])
                srv.send_message(msg, to_addrs=recipients)
            finally:
                try:
                    srv.quit()
                except Exception:  # noqa: BLE001
                    pass
        return {"ok": True, "to": to, "cc": cc, "subject": subject}
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


def _body_snippet(m) -> str:
    """First ~500 chars of the plain-text body; attachments soft-skipped."""
    try:
        if m.is_multipart():
            for part in m.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                if "attachment" in str(part.get("Content-Disposition") or "").lower():
                    continue
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        return _to_text(payload, part).strip()[:_SNIPPET_CAP]
            return ""
        payload = m.get_payload(decode=True)
        if payload:
            return _to_text(payload, m).strip()[:_SNIPPET_CAP]
        return str(m.get_payload() or "")[:_SNIPPET_CAP]
    except Exception:  # noqa: BLE001
        return ""


def _first_bytes(msg_data) -> bytes:
    for part in msg_data or []:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(
                part[1], (bytes, bytearray)):
            return bytes(part[1])
    return b""


def email_read(args: dict, cwd: str | None = None) -> dict:
    """Fetch recent/matching emails via IMAP. ``args``: optional ``folder``
    (default INBOX), ``limit`` (default 10), ``query``/``search`` (substring
    matched against subject+from), ``unseen_only`` (bool). Newest first."""
    if _disabled():
        return {"ok": False, "error": "email disabled (AIFORGE_EMAIL_DISABLE=1)"}
    c = _imap_conf()
    if not c["host"]:
        return {"ok": False,
                "error": "email not configured (set AIFORGE_IMAP_HOST/USER/PASSWORD)"}
    folder = str(args.get("folder") or "INBOX")
    try:
        limit = max(1, int(args.get("limit", 10) or 10))
    except (TypeError, ValueError):
        limit = 10
    query = str(args.get("query") or args.get("search") or "").strip().lower()
    unseen = _truthy(args.get("unseen_only"))
    try:
        if c["ssl"]:
            srv = imaplib.IMAP4_SSL(c["host"], c["port"])
        else:
            srv = imaplib.IMAP4(c["host"], c["port"])
        try:
            if c["user"]:
                srv.login(c["user"], c["password"])
            srv.select(folder)
            criteria = ["UNSEEN"] if unseen else ["ALL"]
            typ, data = srv.search(None, *criteria)
            ids = (data[0].split() if data and data[0] else [])
            ids = list(reversed(ids))  # newest (highest seq) first
            out: list[dict] = []
            for num in ids:
                if len(out) >= limit:
                    break
                try:
                    typ, msg_data = srv.fetch(num, "(RFC822)")
                except Exception:  # noqa: BLE001 — skip an unreadable message
                    continue
                raw = _first_bytes(msg_data)
                if not raw:
                    continue
                m = _email.message_from_bytes(raw)
                frm = _decode_header(m.get("From", ""))
                subj = _decode_header(m.get("Subject", ""))
                if query and query not in (subj.lower() + " " + frm.lower()):
                    continue
                out.append({
                    "uid": num.decode() if isinstance(num, bytes) else str(num),
                    "from": frm,
                    "to": _decode_header(m.get("To", "")),
                    "subject": subj,
                    "date": m.get("Date", ""),
                    "snippet": _body_snippet(m),
                })
            try:
                srv.close()
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True, "messages": out}
        finally:
            try:
                srv.logout()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 — never raise into the agent loop
        return {"ok": False, "error": str(exc)}


# email_search is a thin alias — email_read already accepts ``query``.
def email_search(args: dict, cwd: str | None = None) -> dict:
    """Alias of :func:`email_read` (kept for a natural verb the model may emit)."""
    return email_read(args, cwd)


__all__ = ["email_send", "email_read", "email_search"]
