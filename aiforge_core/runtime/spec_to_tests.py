"""Spec → failing-test scaffold (standards gap C5).

Parse the ticket body's "Acceptance" / "Acceptance criteria" bullets
and emit a one-file failing-test scaffold so the Doer has a TDD
target. KISS: pure text → pure file write; the Verifier/Refiner does
the actual judging, we just plant the seed.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger("aiforge.spec_to_tests")

_ACCEPTANCE_HEAD = re.compile(
    r"(?im)^#{0,3}\s*(acceptance(?:\s+criteria)?|definition\s+of\s+done)\b",
)
_BULLET = re.compile(r"^\s*[-*]\s+(.+?)\s*$", re.M)


def _extract_acceptance(body: str) -> list[str]:
    """Pull bulleted acceptance lines under any 'Acceptance' heading."""
    if not body:
        return []
    m = _ACCEPTANCE_HEAD.search(body)
    if not m:
        return []
    # Slice from heading to next blank-line block / next heading.
    tail = body[m.end():]
    stop = re.search(r"\n\s*\n#", tail) or re.search(r"\n\s*\n[^-*]", tail)
    block = tail[: stop.start()] if stop else tail
    return [b.group(1).strip() for b in _BULLET.finditer(block)]


def write_scaffold(
    ticket_identifier: str,
    ticket_body: str,
    *,
    repo_root: str,
    language: str = "python",
) -> dict:
    """Write a failing-test scaffold under ``tests/aiforge_spec/``.

    Returns ``{ok, path?, bullets, language, error?}``. Soft-fails;
    never raises.

    The Doer's `test_runner` tool will pick it up automatically (the
    standard pytest discover path includes ``tests/``). Convention:
    one `test_<TICKET>.py` (or `.test.js` / `_test.go`) per ticket.
    """
    bullets = _extract_acceptance(ticket_body or "")
    if not bullets:
        return {"ok": False, "error": "no_acceptance"}
    out_dir = Path(repo_root) / "tests" / "aiforge_spec"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"mkdir: {exc}"}
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", ticket_identifier.lower()).strip("_")
    if language == "python":
        fname = f"test_{slug}.py"
        body_text = _python_scaffold(ticket_identifier, bullets)
    elif language in {"javascript", "typescript", "node"}:
        fname = f"{slug}.spec.js"
        body_text = _js_scaffold(ticket_identifier, bullets)
    elif language == "go":
        fname = f"{slug}_test.go"
        body_text = _go_scaffold(ticket_identifier, bullets)
    else:
        fname = f"test_{slug}.md"
        body_text = _markdown_scaffold(ticket_identifier, bullets)
    fp = out_dir / fname
    if fp.exists():
        return {"ok": True, "path": str(fp), "bullets": bullets,
                "language": language, "preexisting": True}
    try:
        fp.write_text(body_text, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"write: {exc}"}
    return {"ok": True, "path": str(fp), "bullets": bullets,
            "language": language, "preexisting": False}


def _python_scaffold(ticket: str, bullets: list[str]) -> str:
    lines = [
        f'"""Auto-generated acceptance scaffold for {ticket}.',
        "Each test is a failing stub — Doer makes it pass.",
        '"""',
        "import pytest",
        "",
        "",
    ]
    for i, b in enumerate(bullets, 1):
        safe = re.sub(r"[^a-zA-Z0-9 ]+", " ", b)[:60].strip()
        name = re.sub(r"\s+", "_", safe).lower() or f"criterion_{i}"
        lines.append(f"def test_{i:02d}_{name}() -> None:")
        lines.append(f"    # acceptance: {b}")
        lines.append('    pytest.skip("aiforge: replace skip with real assertion")')
        lines.append("")
    return "\n".join(lines)


def _js_scaffold(ticket: str, bullets: list[str]) -> str:
    lines = [f"// Auto-generated acceptance scaffold for {ticket}.", ""]
    lines.append(f"describe('{ticket}', () => {{")
    for i, b in enumerate(bullets, 1):
        lines.append(f"  it.todo('{i:02d}: {b.replace(chr(39), chr(96))}');")
    lines.append("});")
    return "\n".join(lines)


def _go_scaffold(ticket: str, bullets: list[str]) -> str:
    lines = ["package aiforge_spec", "", "import \"testing\"", ""]
    for i, b in enumerate(bullets, 1):
        safe = re.sub(r"[^A-Za-z0-9]+", "", b.title())[:40] or f"Criterion{i}"
        lines.append(f"func Test{ticket.replace('-', '')}_{i:02d}_{safe}(t *testing.T) {{")
        lines.append(f"  // acceptance: {b}")
        lines.append("  t.Skip(\"aiforge: replace skip with real assertion\")")
        lines.append("}")
        lines.append("")
    return "\n".join(lines)


def _markdown_scaffold(ticket: str, bullets: list[str]) -> str:
    lines = [f"# {ticket} — acceptance scaffold", ""]
    for i, b in enumerate(bullets, 1):
        lines.append(f"- [ ] {i:02d}: {b}")
    return "\n".join(lines)


__all__ = ["write_scaffold"]
