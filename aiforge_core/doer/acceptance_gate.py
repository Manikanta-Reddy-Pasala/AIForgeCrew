"""Acceptance-gate final_answer check for Doer.

Parses the ``## Acceptance`` section from the ticket body, extracts a list
of bullet items, then on each ``final_answer`` attempt verifies the union
of allowed files contains the key tokens of every acceptance bullet.

Returns ``True`` to allow final_answer, raises ``AssertionError`` to
force smolagents to keep iterating. Smolagents catches the error and
feeds it back to the model as an Observation, so the message we raise
becomes the model's next prompt context.
"""
from __future__ import annotations

import os
import re
from typing import Iterable


_ACCEPTANCE_HEADERS = ("## acceptance", "## acceptance criteria", "## done when")


def parse_acceptance_bullets(body: str) -> list[str]:
    """Extract bullet lines from ``## Acceptance`` section."""
    if not body:
        return []
    if "\\n" in body and "\n" not in body:
        body = body.replace("\\n", "\n")
    lower = body.lower()
    idx = -1
    for header in _ACCEPTANCE_HEADERS:
        i = lower.find(header)
        if i >= 0:
            idx = i
            break
    if idx < 0:
        return []
    nl = body.find("\n", idx)
    if nl < 0:
        return []
    section = body[nl + 1:]
    end = section.find("\n## ")
    if end >= 0:
        section = section[:end]
    bullets: list[str] = []
    for line in section.splitlines():
        s = line.strip()
        if not s:
            if bullets:
                break
            continue
        if s.startswith("##"):
            break
        m = re.match(r"^\d+[.)]\s+(.*)$", s)
        if m:
            bullets.append(m.group(1).strip())
            continue
        if s.startswith(("-", "*", "•")):
            bullets.append(s.lstrip("-*• ").strip())
    return bullets


_NOISE = {
    "the", "a", "an", "and", "or", "for", "with", "to", "in", "of",
    "is", "are", "be", "should", "must", "must:", "add", "ensure",
    "verify", "if", "not", "any", "all", "missing", "endpoint",
    "method", "field", "import", "imports", "compile", "green",
}


def _tokens(s: str) -> set[str]:
    """Pull identifier-ish tokens out of an acceptance bullet."""
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_.()`@/]+", s)
    out: set[str] = set()
    for tok in raw:
        t = tok.strip("`.,")
        if not t or t.lower() in _NOISE:
            continue
        if len(t) < 3:
            continue
        out.add(t)
    return out


def _read_files(worktree_path: str, allowed: Iterable[str]) -> str:
    """Concatenate the contents of every allowed file inside the worktree.

    Resolves glob entries (``foo/**`` etc.) against the worktree.
    """
    blob = []
    for entry in allowed:
        entry = entry.strip()
        if not entry:
            continue
        candidate = os.path.join(worktree_path, entry)
        if os.path.isfile(candidate):
            try:
                blob.append(open(candidate, "r", encoding="utf-8",
                                  errors="replace").read())
            except Exception:
                pass
            continue
        # Glob form — walk the worktree
        prefix = re.split(r"/\*+", entry, maxsplit=1)[0]
        root = os.path.join(worktree_path, prefix)
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f.endswith((".java", ".py", ".ts", ".tsx", ".js",
                               ".kt", ".go", ".rs", ".md", ".yaml", ".yml")):
                    try:
                        blob.append(open(os.path.join(dirpath, f), "r",
                                         encoding="utf-8",
                                         errors="replace").read())
                    except Exception:
                        pass
    return "\n".join(blob)


def make_acceptance_check(
    ticket_body: str,
    worktree_path: str,
    allowed: set[str],
    *,
    coverage_threshold: float = 0.5,
):
    """Return a final_answer_check callable.

    For each acceptance bullet, compute the fraction of identifier tokens
    that appear somewhere in the allowed-files blob. Bullet passes if
    coverage >= ``coverage_threshold``. All bullets must pass for
    final_answer to be accepted.
    """
    bullets = parse_acceptance_bullets(ticket_body)
    if not bullets:
        return None

    def check(final_answer, memory, agent=None) -> bool:
        blob = _read_files(worktree_path, allowed)
        missing: list[str] = []
        for b in bullets:
            toks = _tokens(b)
            if not toks:
                continue
            hit = sum(1 for t in toks if t in blob)
            cov = hit / len(toks)
            if cov < coverage_threshold:
                missing.append(f"  - {b!r} (coverage {cov:.0%}, "
                               f"tokens missing: "
                               f"{sorted(t for t in toks if t not in blob)})")
        if missing:
            raise AssertionError(
                "Acceptance gate: final_answer rejected — "
                f"{len(missing)}/{len(bullets)} acceptance bullets not yet "
                "reflected in the allowed files. Re-read the file, then add "
                "the missing pieces with edit_block before calling "
                "final_answer again. Missing:\n" + "\n".join(missing)
            )
        return True

    check.__name__ = "acceptance_gate"
    return check
