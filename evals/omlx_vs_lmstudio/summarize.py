#!/usr/bin/env python3
"""Aggregate per-cell JSON results into a side-by-side Markdown table.

Usage: summarize.py <results_dir>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def fmt(v, prec=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{prec}f}"
    return str(v)


def main(path: Path) -> None:
    cells: dict[tuple[str, str, str, str], dict] = {}
    for f in sorted(path.glob("*.json")):
        d = json.loads(f.read_text())
        m = d["meta"]
        cells[(m["server"], m["model"], m["domain"], m["phase"])] = d.get("summary", {})

    domains = sorted({k[2] for k in cells})
    models_by_server = {}
    for (server, model, _, _) in cells:
        models_by_server.setdefault(server, set()).add(model)

    out = ["# oMLX vs LM Studio — Bench Summary", ""]
    out.append(f"Result dir: `{path}`")
    out.append("")

    for domain in domains:
        out.append(f"## Domain: `{domain}`")
        out.append("")
        out.append("| Server | Model | Phase | TTFT p50 (s) | Decode tok/s p50 | Total p50 (s) | Cache speedup (B) | Agg tok/s (C) |")
        out.append("|---|---|---|---|---|---|---|---|")
        for (server, model, d, phase), s in sorted(cells.items()):
            if d != domain:
                continue
            ttft = (s.get("ttft_s") or {}).get("p50")
            dec = (s.get("decode_tok_s") or {}).get("p50")
            tot = (s.get("total_s") or {}).get("p50")
            cs = s.get("cache_speedup_ratio")
            agg = s.get("aggregate_tok_s_across_batches")
            out.append(
                f"| {server} | `{model}` | {phase} | "
                f"{fmt(ttft, 3)} | {fmt(dec)} | {fmt(tot, 2)} | {fmt(cs, 2)} | {fmt(agg)} |"
            )
        out.append("")

    out.append("## Decision Rule Snapshot")
    out.append("- Phase A tie (±10%) expected; check raw numbers above.")
    out.append("- Phase B `cache_speedup_ratio` >> 1 means the server reused KV across turns.")
    out.append("- Phase C `aggregate_tok_s_across_batches` is the continuous-batching win.")
    print("\n".join(out))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
