"""After an edit: when the work adds a call to another service, remind the
model once of what such code needs to work in production.

A real job asked for a small class that tells another service when a user's
password changes. Every attempt posted the request, and every one of them
broke the caller: an untrimmed token made an invalid header that threw, an
empty URL threw, an error from the other side propagated into the business
flow. The other agent's version skipped cleanly when unconfigured and did
not throw, and passed where ours did not. These are not test concerns: they
are what makes notifier/client code correct.

One note per turn, only when a non-test file GAINS an outbound HTTP call (it
did not have one at the work's base). AIFORGE_CHAT_OUTBOUND_NOTE=0 turns it off.
"""
from __future__ import annotations

import os
import re

from ._stub_note import _ASIDE, _text_at, _top

#: Outbound HTTP clients across common stacks.
_CALLS = re.compile(
    r"\bWebClient\b|\bRestTemplate\b|\bRestClient\b|\bHttpClient\b|HttpURLConnection|"
    r"\bOkHttpClient\b|\bFeignClient\b|\brequests\.(?:get|post|put|patch|delete)\(|"
    r"\bhttpx\.|\baiohttp\.ClientSession|\burllib\.request|\bfetch\(|\baxios\.|"
    r"\breqwest::|\bhttp\.(?:Post|Get|NewRequest)\(|\bHttpWebRequest\b|\bnet/http\b")

NOTE = (
    "[automated review — not the user] This change adds a call to another service. "
    "Make the calling code correct in production, not only on the happy path:\n"
    "1. Not configured (empty/blank URL, key or token) → do nothing, quietly.\n"
    "2. A failure of the call (refused, error status, timeout, bad header) must not "
    "throw into the caller's business flow: catch it and log it.\n"
    "3. Clean config values before use (trim whitespace and newlines).\n"
    "4. Check the values you send (ids, phone numbers, emails) and skip ones that "
    "are not valid instead of sending them.\n"
    "5. Use the endpoint you were given as it is; do not invent a path for it.\n"
    "6. Do not retry a request the other side refused (4xx).\n"
    "Apply what fits this code; keep it small.")


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_OUTBOUND_NOTE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _gained_call(full: str, base) -> bool:
    top = _top(full)
    if not top or not os.path.isfile(full):
        return False
    rel = os.path.relpath(os.path.realpath(full), os.path.realpath(top))
    if _ASIDE.search(rel.replace(os.sep, "/")):
        return False
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            now = fh.read()
    except OSError:
        return False
    if not _CALLS.search(now):
        return False
    before = _text_at(base, top, rel)
    return not (before and _CALLS.search(before))


def check(st, files, cwd):
    """Yield a chat line and hold the note, once per turn."""
    if not _on() or not hasattr(st, "__dict__") or st.__dict__.get("outbound_noted"):
        return
    from ._contract_gate import work_base
    base = work_base(st)
    for shown, full in files:
        try:
            hit = _gained_call(full, base)
        except Exception:  # noqa: BLE001 — a check never breaks a step
            continue
        if not hit:
            continue
        st.outbound_noted = True
        yield {"type": "thought", "role": "system",
               "text": f"🔌 {shown} now calls another service — reminding the model "
                       "of unconfigured / failure / input handling"}
        from ._notes import hold
        hold(st, NOTE)
        return
