"""Tally ↔ OneShell trial-balance reconciliation — thin delegate to PDS.

The actual TB diff/reconcile logic lives in PosDataSyncService at
``POST /api/v1/data/tally-ingest/compare-tb-files`` (Java service,
file-vs-file diff with optional DB-context enrichment when a
businessId is supplied). PDS is the source of truth: it ships fixes
(GROUP rollup, BigDecimal precision, DB-context enrichment, sign
repair, etc.) on its own release cadence and we don't want a parallel
Python copy that drifts.

This module keeps the two AIForge-side entry points stable:

* :func:`run_workflow` — invoked by ``WorkflowRegistry`` when a
  ticket matches the ``tally-trial-balance`` triggers. Reads the two
  attachments off the ticket, multipart-posts them to PDS, formats
  the response as Markdown for the audit-report artifact.

* :func:`main` — ``aiforge-agents-tb`` CLI, same flow but takes file
  paths off argv and prints the report to stdout.

PDS endpoint discovery via env::

    AIFORGE_PDS_API_BASE   default http://localhost:8092/api/v1/data/tally-ingest

CLI:

    aiforge-agents-tb \\
        --tally    ~/Downloads/tally-acme-2026.xlsx \\
        --oneshell ~/Downloads/oneshell-acme-2026.csv \\
        --business b117695104178401
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _pds_base() -> str:
    return os.environ.get(
        "AIFORGE_PDS_API_BASE",
        "http://localhost:8092/api/v1/data/tally-ingest",
    ).rstrip("/")


def call_pds_compare(
    tally_path: Path,
    oneshell_path: Path,
    *,
    business_id: str = "",
    timeout_s: float = 120.0,
) -> dict:
    """POST both files to PDS ``/compare-tb-files`` and return the JSON.

    Empty ``business_id`` is allowed — PDS runs a pure file-vs-file
    diff in that case (no DB-context enrichment for unmatched leaves).
    Raises :class:`OSError` on transport failure or non-2xx response so
    the caller can map it to a workflow error.
    """
    import httpx

    url = _pds_base() + "/compare-tb-files"
    headers: dict[str, str] = {}
    if business_id:
        headers["X-Business-Id"] = business_id
    with open(tally_path, "rb") as t_fh, open(oneshell_path, "rb") as o_fh:
        files = {
            "tallyTb": (tally_path.name, t_fh, "application/octet-stream"),
            "oneshellTb": (oneshell_path.name, o_fh,
                           "application/octet-stream"),
        }
        try:
            r = httpx.post(url, files=files, headers=headers,
                           timeout=timeout_s)
        except httpx.HTTPError as exc:
            raise OSError(f"PDS unreachable at {url}: {exc}") from exc
    if r.status_code >= 300:
        raise OSError(
            f"PDS returned {r.status_code}: {(r.text or '')[:300]}"
        )
    return r.json() or {}


def _render_summary(summary: dict, lines: list[str]) -> None:
    if not summary:
        return
    lines.append("## Summary\n")
    for k, v in summary.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")


def _render_buckets(buckets: dict, lines: list[str]) -> None:
    lines.append("## Buckets\n")
    for name, rows in buckets.items():
        count = len(rows) if isinstance(rows, list) else "?"
        lines.append(f"### {name} ({count})\n")
        if isinstance(rows, list) and rows:
            # First 5 rows as a peek; full data stays in the doer_outcome.raw
            # payload for any audit downstream.
            for row in rows[:5]:
                lines.append(f"- `{json.dumps(row, default=str)[:300]}`")
            if len(rows) > 5:
                lines.append(f"- … and {len(rows) - 5} more")
        lines.append("")


def render_markdown(result: dict) -> str:
    """Format the PDS response as a Markdown audit report.

    PDS's response schema (per v1.4 release) is intentionally self-describing:
    top-level ``summary`` + ``buckets`` lists. We surface the bucket names +
    counts + a few representative rows rather than dumping the whole JSON —
    operators read the report, raw JSON stays in the doer-outcome dict.
    """
    lines: list[str] = ["# Tally ↔ OneShell trial-balance reconciliation\n"]
    summary = result.get("summary") or {}
    _render_summary(summary, lines)
    buckets = result.get("buckets") or {}
    if isinstance(buckets, dict):
        _render_buckets(buckets, lines)
    if not summary and not buckets:
        # Schema we didn't expect — dump the raw JSON so the operator at least
        # sees what PDS returned.
        lines.append("## Raw PDS response\n")
        lines.append("```json")
        lines.append(json.dumps(result, indent=2, default=str)[:4000])
        lines.append("```")
    return "\n".join(lines) + "\n"


def _has_material_gap(result: dict) -> bool:
    """Heuristic: anything in the ``large`` / ``mismatched`` buckets
    counts as a material gap. PDS schema may rename these — when in
    doubt, treat the run as blocking so the human triage path runs."""
    buckets = result.get("buckets") or {}
    if not isinstance(buckets, dict):
        return False
    for name, rows in buckets.items():
        lname = str(name).lower()
        if not isinstance(rows, list) or not rows:
            continue
        if any(k in lname for k in ("large", "mismatch", "missing", "delta")):
            return True
    summary = result.get("summary") or {}
    if isinstance(summary, dict):
        if summary.get("hasMaterialGap") is True:
            return True
    return False


# ─────────────────────────── workflow entry ─────────────────────────────


def run_workflow(ticket: dict, *, log=None) -> dict:
    """Workflow handler — POST attachments to PDS, return doer-outcome.

    Stable contract — preserved from the legacy in-process Python copy
    so the WorkflowRegistry dispatcher and downstream agents don't
    have to change.
    """
    from aiforge_core.memory import online_learner as _learner

    ticket_id = ticket.get("identifier") or ticket.get("id") or ""
    title = ticket.get("title") or ""
    body = ticket.get("body") or ""
    text = (title + " " + body).lower()

    def _emit(event: str, **fields):
        if log is None:
            return
        try:
            from aiforge_core.observability.logging import emit as _evt
            _evt(log, event, ticket=ticket_id, **fields)
        except Exception:
            pass

    atts = _learner.attachments_for(str(ticket_id))
    by_role: dict[str, dict] = {}
    for a in atts:
        by_role.setdefault(a["role"], a)

    missing = [r for r in ("tally", "oneshell") if r not in by_role]
    if missing:
        _emit("trial_balance.missing_attachments", missing=missing)
        return {
            "artifact_type": "doer_outcome",
            "process": "trial_balance",
            "applied": False,
            "udiff": "",
            "target": "trial-balance-report.md",
            "problems": [{
                "mode": "missing_attachment",
                "evidence": f"required attachment role(s) missing: {missing}",
            }],
            "blocked_by_detectors": True,
        }

    # Best-effort businessId scrape from ticket text. PDS accepts blank
    # and degrades to file-vs-file only (no DB context enrichment).
    import re as _re
    m = _re.search(r"\bb\d{14,}\b", text)
    business_id = m.group(0) if m else ""

    try:
        result = call_pds_compare(
            Path(by_role["tally"]["file_path"]),
            Path(by_role["oneshell"]["file_path"]),
            business_id=business_id,
        )
    except OSError as exc:
        _emit("trial_balance.pds_unreachable", error=str(exc)[:200])
        return {
            "artifact_type": "doer_outcome",
            "process": "trial_balance",
            "applied": False,
            "udiff": "",
            "target": "trial-balance-report.md",
            "problems": [{
                "mode": "pds_unreachable",
                "evidence": str(exc)[:500],
            }],
            "blocked_by_detectors": True,
        }

    md = render_markdown(result)
    blocked = _has_material_gap(result)
    _emit(
        "trial_balance.pds_done",
        blocked=blocked,
        business_id=business_id or "",
    )
    return {
        "artifact_type": "doer_outcome",
        "process": "trial_balance",
        "mode": "pds-delegate",
        "applied": False,
        "udiff": md,
        "target": "trial-balance-report.md",
        "raw": result,
        "problems": [],
        "blocked_by_detectors": blocked,
        "business_id": business_id or None,
    }


# ─────────────────────────── CLI entry ──────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """``aiforge-agents-tb`` CLI — thin wrapper around PDS compare."""
    p = argparse.ArgumentParser(
        prog="aiforge-agents-tb",
        description=(
            "Compare a Tally TB export against a OneShell TB export by "
            "delegating to PosDataSyncService /compare-tb-files."
        ),
    )
    p.add_argument("--tally", required=True, help="Path to Tally TB xlsx")
    p.add_argument("--oneshell", required=True,
                   help="Path to OneShell TB export (xlsx/csv)")
    p.add_argument("--business", default="",
                   help="Business id for DB-context enrichment (optional)")
    p.add_argument("--json", action="store_true",
                   help="Print PDS raw JSON instead of the Markdown report")
    args = p.parse_args(argv)

    try:
        result = call_pds_compare(
            Path(args.tally).expanduser(),
            Path(args.oneshell).expanduser(),
            business_id=args.business,
        )
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render_markdown(result))
    return 1 if _has_material_gap(result) else 0


if __name__ == "__main__":
    sys.exit(main())
