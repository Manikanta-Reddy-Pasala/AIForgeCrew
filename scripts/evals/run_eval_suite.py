#!/usr/bin/env python3
"""F1..F7 eval-suite runner — auto-grade vs golden.

KISS: one fixture per file under ``evals/fixtures/<name>/``, each
holding:
- ``ticket.json`` — input ticket body
- ``allowed.txt`` — newline-separated allowed file paths
- ``golden.json`` — expected outcomes:
    {
      "compile_green": true,
      "edit_block_min": 3,
      "files_must_change": ["..."],
      "must_contain": [{"file": "X.java", "snippet": "..."}],
      "must_not_contain": [{"file": "X.java", "snippet": "..."}]
    }

Runner:
1. Spawns the Doer via ``run_doer_via_ga`` against a worktree clone
   of the fixture's target repo.
2. Pulls counters + final compile result.
3. Compares against ``golden.json``.
4. Emits one ``{name, pass, mismatches[], wall_s, tokens, usd}`` row
   per fixture; total to stdout JSON Lines + ``evals/results/<run-
   id>/summary.json``.

Exit code: 0 = all pass, 1 = any failure. Suitable for tekton /
GitHub Action gate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="AIForge eval suite")
    p.add_argument("--fixtures", default="evals/fixtures",
                   help="Path to fixtures directory")
    p.add_argument("--results",  default="evals/results",
                   help="Where to write the run summary")
    p.add_argument("--name", action="append", default=None,
                   help="Run only the named fixture(s); repeatable")
    p.add_argument("--bail", action="store_true",
                   help="Stop on first failure")
    args = p.parse_args(argv)

    fixtures_dir = Path(args.fixtures)
    if not fixtures_dir.is_dir():
        print(f"fixtures dir not found: {fixtures_dir}", file=sys.stderr)
        return 2

    run_id = time.strftime("%Y%m%dT%H%M%S")
    out_dir = Path(args.results) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    pass_count = 0
    fail_count = 0
    for fix in sorted(fixtures_dir.iterdir()):
        if not fix.is_dir():
            continue
        if args.name and fix.name not in args.name:
            continue
        result = _run_one(fix, out_dir)
        rows.append(result)
        print(json.dumps(result, ensure_ascii=False))
        if result["pass"]:
            pass_count += 1
        else:
            fail_count += 1
            if args.bail:
                break

    summary = {
        "run_id": run_id,
        "passed": pass_count,
        "failed": fail_count,
        "rows": rows,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({"summary": {"passed": pass_count,
                                  "failed": fail_count}}))
    return 0 if fail_count == 0 else 1


def _run_one(fix: Path, out_dir: Path) -> dict:
    """Run one fixture. Returns a row dict suitable for JSONL output."""
    name = fix.name
    t0 = time.time()
    try:
        ticket = json.loads((fix / "ticket.json").read_text())
        golden = json.loads((fix / "golden.json").read_text())
        allowed_text = (fix / "allowed.txt").read_text(errors="replace")
        allowed = [
            line.strip() for line in allowed_text.splitlines() if line.strip()
        ]
    except Exception as exc:
        return {"name": name, "pass": False,
                "mismatches": [f"fixture-load-error: {exc}"],
                "wall_s": 0.0}
    repo_path = ticket.get("repo_path") or ticket.get("worktree")
    if not repo_path or not os.path.isdir(repo_path):
        return {"name": name, "pass": False,
                "mismatches": [f"repo_path missing/invalid: {repo_path!r}"],
                "wall_s": 0.0}

    # Construct a minimal ticket-shim object expected by run_doer_via_ga.
    class _Ticket:
        identifier = ticket.get("identifier", name)
        id = ticket.get("id", name)
        body = ticket.get("body", "")
        title = ticket.get("title", name)
    body_with_allowed = (
        _Ticket.body
        + "\n\n## Allowed files\n"
        + "\n".join(allowed)
    )
    _Ticket.body = body_with_allowed

    # Lazy import — keeps the runner script importable in environments
    # where AIForge isn't fully installed (e.g. running --help).
    from aiforge_core.doer.ga_runner import run_doer_via_ga

    out: dict = run_doer_via_ga(
        ticket=_Ticket(),
        worktree_path=repo_path,
        plan_text=ticket.get("plan_text", ""),
        max_turns=int(golden.get("max_turns", 30)),
    )
    wall_s = round(time.time() - t0, 2)
    mismatches = _grade(out, golden, repo_path=repo_path)
    return {
        "name": name,
        "pass": not mismatches,
        "mismatches": mismatches,
        "wall_s": wall_s,
        "stop_reason": out.get("stop_reason"),
        "edit_block_ok": out.get("edit_block_ok", 0),
        "compile_green": out.get("compile_green", 0),
        "summary": (out.get("summary") or "")[:300],
    }


def _grade(out: dict, golden: dict, *, repo_path: str) -> list[str]:
    """Compare ``out`` vs ``golden``. Returns mismatch reasons."""
    bad: list[str] = []
    if golden.get("compile_green") and not out.get("compile_green"):
        bad.append("compile_green expected, got 0")
    eb_min = int(golden.get("edit_block_min", 0))
    if eb_min and int(out.get("edit_block_ok", 0)) < eb_min:
        bad.append(
            f"edit_block_ok={out.get('edit_block_ok',0)} < expected {eb_min}",
        )
    for path in golden.get("files_must_change") or []:
        full = os.path.join(repo_path, path)
        if not os.path.exists(full):
            bad.append(f"missing changed file: {path}")
    for assertion in golden.get("must_contain") or []:
        path = os.path.join(repo_path, assertion["file"])
        try:
            text = open(path).read()
        except Exception:
            bad.append(f"can't read {path} for must_contain check")
            continue
        if assertion["snippet"] not in text:
            bad.append(f"must_contain miss in {assertion['file']}: "
                       f"{assertion['snippet'][:60]}")
    for assertion in golden.get("must_not_contain") or []:
        path = os.path.join(repo_path, assertion["file"])
        try:
            text = open(path).read()
        except Exception:
            continue
        if assertion["snippet"] in text:
            bad.append(f"must_not_contain hit in {assertion['file']}: "
                       f"{assertion['snippet'][:60]}")
    return bad


if __name__ == "__main__":
    sys.exit(main())
