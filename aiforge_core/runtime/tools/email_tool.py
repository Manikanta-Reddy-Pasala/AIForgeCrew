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


# ─────────────────────────── connectivity test ──────────────────────

def email_test() -> dict:
    """Connectivity + auth check for the Settings UI. Connects (and logs in,
    when a user is set) to whichever of SMTP/IMAP is configured. Returns
    ``{"ok": bool, "smtp": {...}, "imap": {...}}`` — ``ok`` is true when every
    *configured* side connected. Never raises."""
    if _disabled():
        return {"ok": False, "error": "email disabled (AIFORGE_EMAIL_DISABLE=1)"}
    smtp_c, imap_c = _smtp_conf(), _imap_conf()
    if not smtp_c["host"] and not imap_c["host"]:
        return {"ok": False, "error": "email_not_configured",
                "hint": "set an SMTP host (send) and/or an IMAP host (read)"}

    out: dict = {"smtp": None, "imap": None}
    ok = True

    if smtp_c["host"]:
        try:
            if smtp_c["starttls"]:
                srv = smtplib.SMTP(smtp_c["host"], smtp_c["port"], timeout=_TIMEOUT_S)
                try:
                    srv.ehlo(); srv.starttls(); srv.ehlo()
                    if smtp_c["user"]:
                        srv.login(smtp_c["user"], smtp_c["password"])
                finally:
                    try: srv.quit()
                    except Exception: pass  # noqa: BLE001,E701
            else:
                srv = smtplib.SMTP_SSL(smtp_c["host"], smtp_c["port"], timeout=_TIMEOUT_S)
                try:
                    if smtp_c["user"]:
                        srv.login(smtp_c["user"], smtp_c["password"])
                finally:
                    try: srv.quit()
                    except Exception: pass  # noqa: BLE001,E701
            out["smtp"] = {"ok": True, "host": smtp_c["host"], "port": smtp_c["port"],
                           "user": smtp_c["user"]}
        except Exception as exc:  # noqa: BLE001
            ok = False
            out["smtp"] = {"ok": False, "host": smtp_c["host"], "error": str(exc)}

    if imap_c["host"]:
        try:
            if imap_c["ssl"]:
                srv = imaplib.IMAP4_SSL(imap_c["host"], imap_c["port"])
            else:
                srv = imaplib.IMAP4(imap_c["host"], imap_c["port"])
            try:
                if imap_c["user"]:
                    srv.login(imap_c["user"], imap_c["password"])
            finally:
                try: srv.logout()
                except Exception: pass  # noqa: BLE001,E701
            out["imap"] = {"ok": True, "host": imap_c["host"], "port": imap_c["port"],
                           "user": imap_c["user"]}
        except Exception as exc:  # noqa: BLE001
            ok = False
            out["imap"] = {"ok": False, "host": imap_c["host"], "error": str(exc)}

    result: dict = {"ok": ok, **out}
    if not ok:
        errs = [v.get("error") for v in (out["smtp"], out["imap"])
                if isinstance(v, dict) and v.get("error")]
        if errs:
            result["error"] = "; ".join(errs)
    return result


__all__ = ["email_send", "email_read", "email_search", "email_test"]
