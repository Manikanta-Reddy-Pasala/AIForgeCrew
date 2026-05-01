"""Tally ↔ OneShell trial-balance reconciliation.

Deterministic — no LLM call. Reads the two files attached to the
ticket (filename matches `*tally*` and `*oneshell*`), joins on
account_code-or-normalised-name, emits MATCH / DIFF / LARGE buckets
+ totals + per-row CSV.

CLI:

    aiforge-agents-tb \\
        --tally  ~/Downloads/tally-acme-2026.xlsx \\
        --oneshell ~/Downloads/oneshell-acme-2026.csv \\
        --env qa --business b117695104178401

The full process spec lives at
`docs/processes/tally-oneshell-trial-balance.md`.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ─────────── normalisation helpers ───────────────────────────────────

_NAME_PUNCT = re.compile(r"[^\w\s]+")
_NAME_WS = re.compile(r"\s+")


def normalise_name(s: str | None) -> str:
    if s is None:
        return ""
    out = str(s).strip().lower()
    out = _NAME_PUNCT.sub(" ", out)
    out = _NAME_WS.sub(" ", out).strip()
    return out


def to_decimal(v: Any) -> float:
    """Tolerant numeric parser. Returns 0.0 for blanks / non-numeric."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("₹", "")
    # Tally exports "(1,000.00)" for negative — wrap in parens
    if s.startswith("(") and s.endswith(")"):
        try:
            return -float(s[1:-1])
        except ValueError:
            return 0.0
    # Trailing CR/DR markers
    suffix = ""
    if s[-2:].upper() in ("CR", "DR"):
        suffix = s[-2:].upper()
        s = s[:-2].strip()
    try:
        n = float(s)
    except ValueError:
        return 0.0
    return -n if suffix == "CR" else n


# ─────────── readers ──────────────────────────────────────────────────

@dataclass
class TBRow:
    code: str = ""
    name: str = ""
    parent: str = ""
    opening: float = 0.0
    debit: float = 0.0
    credit: float = 0.0
    closing: float = 0.0


def _read_table(path: Path) -> list[dict[str, Any]]:
    """Read .csv/.xlsx/.xls into list of {column: value}.

    .xlsx / .xls require the optional `openpyxl` / `xlrd` deps; if
    either is missing the file must be saved as CSV first.
    """
    suf = path.suffix.lower()
    if suf == ".csv":
        with open(path, "r", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    if suf in (".xlsx", ".xlsm"):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise SystemExit(f"openpyxl required for {path.name}: {exc}")
        wb = load_workbook(path, data_only=True, read_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        header = [str(c or "").strip() for c in rows[0]]
        out: list[dict[str, Any]] = []
        for row in rows[1:]:
            out.append({header[i]: row[i] if i < len(row) else "" for i in range(len(header))})
        return out
    if suf == ".xls":
        try:
            import xlrd  # type: ignore
        except ImportError as exc:
            raise SystemExit(f"xlrd required for {path.name}: {exc}")
        wb = xlrd.open_workbook(str(path))
        ws = wb.sheet_by_index(0)
        if ws.nrows == 0:
            return []
        header = [str(ws.cell_value(0, c) or "").strip() for c in range(ws.ncols)]
        return [
            {header[c]: ws.cell_value(r, c) for c in range(ws.ncols)}
            for r in range(1, ws.nrows)
        ]
    raise SystemExit(f"unsupported file type: {path}")


def _pick(d: dict[str, Any], *keys: str) -> Any:
    """First non-empty value for any of the given (case-insensitive) keys."""
    lower = {k.lower(): k for k in d.keys()}
    for k in keys:
        real = lower.get(k.lower())
        if real and d.get(real) not in (None, ""):
            return d[real]
    return ""


def parse_tally(path: Path) -> list[TBRow]:
    """Read a Tally export. Tolerant to header variations."""
    rows = _read_table(path)
    out: list[TBRow] = []
    for r in rows:
        name = str(_pick(r, "Particulars", "Account", "Name", "Ledger") or "").strip()
        if not name:
            continue
        parent = str(_pick(r, "Group", "Parent") or "").strip()
        code = str(_pick(r, "Account Code", "Code", "Ledger Code") or "").strip()
        ob = to_decimal(_pick(r, "Opening Balance", "OB", "Opening"))
        dr = to_decimal(_pick(r, "Debit", "Dr"))
        cr = to_decimal(_pick(r, "Credit", "Cr"))
        cb = to_decimal(_pick(r, "Closing Balance", "Closing", "CB"))
        # skip empty summary rows (no numbers at all)
        if all(v == 0.0 for v in (ob, dr, cr, cb)):
            continue
        out.append(TBRow(
            code=code, name=name, parent=parent,
            opening=ob, debit=dr, credit=cr, closing=cb,
        ))
    return out


def parse_oneshell(path: Path) -> list[TBRow]:
    rows = _read_table(path)
    out: list[TBRow] = []
    for r in rows:
        name = str(_pick(r, "accountName", "name", "Account", "ledger") or "").strip()
        if not name:
            continue
        parent = str(_pick(r, "parentName", "parent") or "").strip()
        code = str(_pick(r, "accountCode", "code") or "").strip()
        ob = to_decimal(_pick(r, "openingBalance", "opening"))
        dr = to_decimal(_pick(r, "periodDebit", "debit", "Dr"))
        cr = to_decimal(_pick(r, "periodCredit", "credit", "Cr"))
        cb = to_decimal(_pick(r, "closingBalance", "closing"))
        if all(v == 0.0 for v in (ob, dr, cr, cb)):
            continue
        out.append(TBRow(
            code=code, name=name, parent=parent,
            opening=ob, debit=dr, credit=cr, closing=cb,
        ))
    return out


# ─────────── reconciliation ───────────────────────────────────────────

@dataclass
class Reconciliation:
    env: str
    business_id: str
    tally_total: float = 0.0
    oneshell_total: float = 0.0
    gap: float = 0.0
    matched: list[dict] = field(default_factory=list)
    diff: list[dict] = field(default_factory=list)
    large: list[dict] = field(default_factory=list)
    tally_only: list[dict] = field(default_factory=list)
    oneshell_only: list[dict] = field(default_factory=list)


_MATCH_TOLERANCE = 1.00
_DIFF_TOLERANCE = 100.00


def _key(row: TBRow) -> str:
    return row.code.strip().lower() if row.code else normalise_name(row.name)


def reconcile(
    tally: list[TBRow], oneshell: list[TBRow],
    *, env: str = "qa", business_id: str = "",
) -> Reconciliation:
    rec = Reconciliation(env=env, business_id=business_id)
    by_t = {_key(r): r for r in tally}
    by_o = {_key(r): r for r in oneshell}

    for k, t in by_t.items():
        rec.tally_total += t.closing
        o = by_o.get(k)
        if o is None:
            rec.tally_only.append(_row_dict(t, "tally_only"))
            continue
        delta_open = round(t.opening - o.opening, 2)
        delta_dr = round(t.debit - o.debit, 2)
        delta_cr = round(t.credit - o.credit, 2)
        delta_close = round(t.closing - o.closing, 2)
        entry = {
            "key": k,
            "tally_name": t.name, "oneshell_name": o.name,
            "code": t.code or o.code,
            "parent": t.parent or o.parent,
            "tally_close": t.closing, "oneshell_close": o.closing,
            "delta_open": delta_open,
            "delta_debit": delta_dr,
            "delta_credit": delta_cr,
            "delta_close": delta_close,
        }
        absd = abs(delta_close)
        if absd <= _MATCH_TOLERANCE:
            entry["bucket"] = "match"
            rec.matched.append(entry)
        elif absd <= _DIFF_TOLERANCE:
            entry["bucket"] = "diff"
            rec.diff.append(entry)
        else:
            entry["bucket"] = "large"
            rec.large.append(entry)
    for k, o in by_o.items():
        rec.oneshell_total += o.closing
        if k not in by_t:
            rec.oneshell_only.append(_row_dict(o, "oneshell_only"))

    rec.gap = round(rec.tally_total - rec.oneshell_total, 2)
    return rec


def _row_dict(r: TBRow, bucket: str) -> dict:
    return {
        "key": _key(r), "name": r.name, "parent": r.parent, "code": r.code,
        "closing": r.closing, "bucket": bucket,
    }


# ─────────── reporting ────────────────────────────────────────────────

def render_markdown(rec: Reconciliation) -> str:
    lines: list[str] = [
        f"## Trial-Balance Reconciliation ({rec.env})",
        "",
        f"- Tally CB total: ₹{rec.tally_total:,.2f}",
        f"- OneShell CB total: ₹{rec.oneshell_total:,.2f}",
        f"- **Gap: ₹{rec.gap:,.2f}**",
        "",
        f"| bucket | count |",
        f"|---|---:|",
        f"| MATCH (|Δ| ≤ ₹1) | {len(rec.matched)} |",
        f"| DIFF (₹1–₹100) | {len(rec.diff)} |",
        f"| LARGE (> ₹100) | {len(rec.large)} |",
        f"| Tally-only | {len(rec.tally_only)} |",
        f"| OneShell-only | {len(rec.oneshell_only)} |",
        "",
    ]
    if rec.large:
        lines.append("### Top 10 LARGE diffs")
        lines.append("| account | code | tally CB | oneshell CB | Δ |")
        lines.append("|---|---|---:|---:|---:|")
        for e in sorted(rec.large, key=lambda x: -abs(x["delta_close"]))[:10]:
            lines.append(
                f"| {e['tally_name'][:60]} | {e.get('code','')} | "
                f"₹{e['tally_close']:,.2f} | ₹{e['oneshell_close']:,.2f} | "
                f"**₹{e['delta_close']:,.2f}** |"
            )
    return "\n".join(lines) + "\n"


def write_csv(rec: Reconciliation, out_path: Path) -> None:
    rows: list[dict] = []
    for e in rec.matched + rec.diff + rec.large:
        rows.append(e)
    for e in rec.tally_only + rec.oneshell_only:
        rows.append(e)
    if not rows:
        return
    fieldnames = sorted({k for r in rows for k in r.keys()})
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ─────────── CLI ──────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aiforge-agents-tb")
    p.add_argument("--tally", required=True)
    p.add_argument("--oneshell", required=True)
    p.add_argument("--env", default="qa", choices=("qa", "prod"))
    p.add_argument("--business", default="")
    p.add_argument("--out-dir", default=".")
    a = p.parse_args(argv)

    t = parse_tally(Path(a.tally))
    o = parse_oneshell(Path(a.oneshell))
    rec = reconcile(t, o, env=a.env, business_id=a.business)

    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{a.env}-{a.business or 'all'}-tb-diff.csv"
    write_csv(rec, csv_path)

    summary = {
        "env": rec.env,
        "business_id": rec.business_id,
        "tally_total": rec.tally_total,
        "oneshell_total": rec.oneshell_total,
        "gap": rec.gap,
        "buckets": {
            "match": len(rec.matched),
            "diff": len(rec.diff),
            "large": len(rec.large),
            "tally_only": len(rec.tally_only),
            "oneshell_only": len(rec.oneshell_only),
        },
        "csv": str(csv_path),
    }
    print(json.dumps(summary, indent=2))
    print()
    print(render_markdown(rec))

    # Block exit code if LARGE diffs exist
    return 1 if (rec.large or abs(rec.gap) > 1000) else 0


if __name__ == "__main__":
    sys.exit(main())
