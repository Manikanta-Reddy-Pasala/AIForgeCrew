"""Credential detection: the half of the filter that must not be wrong.

**A match blocks the whole node**, it does not scrub the matched span. A note
that mentions a credential is a note *about* that credential — its title, the
sentence around it and the file path it names usually identify the system too.
Scrubbing ships all of that, and ships it carrying an implicit claim of safety.
Blocking is also auditable: the operator sees "held back, rule
``secrets.aws_key``" and can go and look at the note itself.

Two halves, on purpose:

* **Known shapes** — precise, essentially free of false positives, and they
  cover the credentials that actually leak: a cloud key pasted out of a console,
  a token copied off a CI page. This is the reliable half.
* **An entropy heuristic** — ``KEY = <long random-looking value>`` where the key
  name reads secret-ish. This is the recall half, and the one to tune from the
  block log, because it is the one that can be wrong in either direction.

The reason string a rule returns names the RULE and never quotes the match. The
block log is written to disk, and a log that records the secret it caught is the
leak it was meant to prevent.
"""
from __future__ import annotations

import math
import re

from aiforge_core.memory.sync.redact import _text

# Known credential shapes. Anchored on the vendor prefix PLUS a length, because
# a prefix alone appears in prose that legitimately explains it — "the AKIA
# prefix identifies an AWS access key id" is knowledge and must sync.
_SHAPES: tuple[tuple[str, re.Pattern], ...] = (
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_\w{50,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("llm_key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}"
                       r"\.[A-Za-z0-9_-]{10,}")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}")),
    ("url_credentials", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@")),
)

# ``key = value`` where the key reads secret-ish. The VALUE is what decides: an
# empty value, a placeholder or a reference is how one CORRECTLY writes about a
# secret, and that note is knowledge worth syncing.
_ASSIGN = re.compile(
    r"\b(?P<key>[A-Za-z0-9_]*"
    r"(?:passwo?rd|passwd|secret|api[_-]?key|apikey|token|credential)s?)"
    r"\s*[:=]\s*(?P<q>[\"']?)(?P<val>[^\s\"'`]{8,})(?P=q)",
    re.IGNORECASE)

# Below this length a value is overwhelmingly a placeholder, and blocking a
# whole node over it costs more knowledge than it protects.
_MIN_SECRET_LEN = 8

# Shannon bits per character above which a value counts as "not a word". Set
# LOW on purpose: this rule only ever looks at a value already assigned to a
# credential-shaped name, so the prior is that it IS a secret, and the job of
# the threshold is only to let an obviously-repetitive placeholder through.
# A real password is often short and unremarkable — ``s3cr3tp4ss`` scores 2.65,
# below any threshold chosen to look "random enough" — and missing one is the
# expensive direction. The reference and placeholder exclusions below are what
# keep documentation syncing, not the entropy number.
_MIN_ENTROPY = 2.5

# A value that reads as an identifier rather than a secret: all lower case with
# underscores, e.g. ``password=from_the_vault``. That is somebody naming WHERE
# the value lives, which is knowledge worth syncing.
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")

# Values that are how one correctly writes about a secret without carrying one.
_PLACEHOLDERS = re.compile(
    r"^(?:\.{3}|x+|<[^>]*>|\{\{?[^}]*\}?\}|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|"
    r"your[_-]?\w+|changeme|redacted|placeholder|example|dummy|none|null|"
    r"true|false|\d+)$",
    re.IGNORECASE)


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _assignment_hit(text: str) -> str:
    """The rule name for a secret-looking assignment, or "".

    A value is a secret when it is long enough, is not a placeholder, is not a
    reference to where the value actually lives, and does not read like prose.
    All four are required: dropping any one of them blocks a note that documents
    configuration, which is precisely the knowledge worth syncing.
    """
    for m in _ASSIGN.finditer(text):
        val = m.group("val")
        if len(val) < _MIN_SECRET_LEN or _PLACEHOLDERS.match(val):
            continue
        if val.startswith(("$", "{", "<", "`")) or _IDENTIFIER.match(val):
            # A reference or a name, not a value: "$VAULT_KEY", "{{ secret }}",
            # "from_the_vault". Documenting where a secret lives is knowledge.
            continue
        if _entropy(val) < _MIN_ENTROPY:
            continue
        return f"secrets.{m.group('key').lower()}"
    return ""


def check(node: dict) -> tuple[str, str]:
    """``(rule, reason)`` when this node carries a credential, else ``("", "")``.

    The reason names the rule and NEVER the match — see the module docstring.
    """
    text = _text.text_of(node)
    for name, pattern in _SHAPES:
        if pattern.search(text):
            return (f"secrets.{name}",
                    "the note contains something shaped like a "
                    f"{name.replace('_', ' ')}")
    rule = _assignment_hit(text)
    if rule:
        return rule, "the note assigns a high-entropy value to a credential-shaped name"
    return "", ""


__all__ = ["check"]
