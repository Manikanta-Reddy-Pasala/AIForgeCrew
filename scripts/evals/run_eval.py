"""Context-strategy eval harness — run a single (track, fixture, sample).

Spawns a fresh ticket on the remote AIForgeCrew API, polls until done,
fetches the llm.call trace, computes metrics, writes JSON.

Track semantics are enforced by the graph-runner's env BEFORE this
script runs (we don't restart the runner here; that's a separate
op step). The track name is recorded in the result file for
bookkeeping only.

Usage:
    python scripts/evals/run_eval.py \\
        --track Z --fixture F1 --samples 3 \\
        --api http://192.168.70.185:8799

Per-run output: evals/results/{track}/{fixture_id}/{seq}.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "evals" / "fixtures"
RESULTS_DIR = REPO_ROOT / "evals" / "results"
DEFAULT_API = os.environ.get("AIFORGE_EVAL_API", "http://192.168.70.185:8799")

TERMINAL_STATUSES = {
    "done", "DONE", "completed", "COMPLETED",
    "error", "ERROR", "failed", "FAILED",
    "cancelled", "canceled", "CANCELLED", "CANCELED",
    # blocked = supervisor halted retries (e.g. feedback verdict=fail, max
    # rounds hit). It's terminal for our eval — no further work happens.
    "blocked", "BLOCKED",
}


def _load_fixture(fid: str) -> dict:
    path = FIXTURES_DIR / f"{fid}.yaml"
    if not path.exists():
        sys.exit(f"fixture not found: {path}")
    with path.open() as fh:
        return yaml.safe_load(fh)


def _post_ticket(client: httpx.Client, fixture: dict) -> dict:
    suffix = uuid.uuid4().hex[:6]
    payload = {
        "title": f"[EVAL] {fixture['title']} [{suffix}]",
        "body": fixture.get("body", ""),
        "assignee_role": fixture.get("assignee_role"),
        "priority": fixture.get("priority", "medium"),
        "project": fixture.get("project"),
        "labels": fixture.get("labels", []),
        "max_turns": fixture.get("max_turns"),
        "metadata": {"eval_fixture": fixture["id"], "eval_run_uuid": suffix},
    }
    r = client.post("/api/tickets", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def _poll_until_done(client: httpx.Client, identifier: str,
                     timeout_s: int = 1200, interval_s: int = 5) -> dict:
    t0 = time.time()
    last_status = ""
    while time.time() - t0 < timeout_s:
        r = client.get(f"/api/tickets/{identifier}", timeout=30)
        r.raise_for_status()
        body = r.json()
        status = body["ticket"]["status"]
        if status != last_status:
            elapsed = int(time.time() - t0)
            print(f"  [{elapsed:>4}s] status={status}")
            last_status = status
        if status in TERMINAL_STATUSES:
            return body
        time.sleep(interval_s)
    raise TimeoutError(f"ticket {identifier} did not finish within {timeout_s}s")


def _fetch_trace(client: httpx.Client, identifier: str) -> list[dict]:
    r = client.get(f"/api/llm-trace/{identifier}", params={"limit": 10000},
                   timeout=60)
    r.raise_for_status()
    body = r.json()
    return body.get("events", [])


def _compute_metrics(trace_events: list[dict],
                     ticket: dict, ticket_events: list[dict],
                     wall_clock_s: float) -> dict:
    """Reduce raw trace + ticket state into one metrics dict."""
    prompt_toks = 0
    completion_toks = 0
    total_toks = 0
    tool_call_counts: dict[str, int] = {}
    read_file_calls = 0
    grep_calls = 0
    graph_rag_calls = 0
    explorer_calls = 0
    llm_call_count = 0

    GRAPH_RAG_TOOLS = {
        "sym_lookup", "impact", "ticket_brief", "cross_repo_flow",
        "dep_graph", "callers", "callees", "find_definition",
        "find_references", "type_hierarchy", "list_symbols",
        "search_symbols", "file_ast", "module_summary",
        "package_summary", "loc_summary", "diff_impact",
        "search_text", "search_doc", "list_files", "list_dirs",
        "describe_repo", "graph_query", "raw_cypher", "ddl",
    }

    total_dur_ms = 0
    estimated_prompt_chars = 0
    estimated_completion_chars = 0
    for ev in trace_events:
        # Events arrive with aiforge fields at top level (logging_setup
        # flattens them); older formats nest under "aiforge". Try both.
        ai = ev.get("aiforge") or ev
        if ev.get("event") != "llm.call":
            continue
        llm_call_count += 1
        total_dur_ms += int(ai.get("dur_ms") or 0)
        usage = ai.get("usage")
        if isinstance(usage, dict) and usage:
            prompt_toks += int(usage.get("prompt_tokens") or 0)
            completion_toks += int(usage.get("completion_tokens") or 0)
            total_toks += int(usage.get("total_tokens") or 0)
        else:
            # mlx_lm doesn't report usage. Fall back to char-length /4
            # estimate for relative-comparison purposes. (Absolute numbers
            # are off by ~10-20% vs tiktoken but cross-track ratios stay
            # meaningful.)
            for m in (ai.get("messages") or []):
                content = m.get("content") if isinstance(m, dict) else None
                if isinstance(content, str):
                    estimated_prompt_chars += len(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            t = part.get("text") or ""
                            if isinstance(t, str):
                                estimated_prompt_chars += len(t)
            resp_obj = ai.get("response") or {}
            resp_content = resp_obj.get("content") if isinstance(resp_obj, dict) else None
            if isinstance(resp_content, str):
                estimated_completion_chars += len(resp_content)
            tcalls_resp = resp_obj.get("tool_calls") if isinstance(resp_obj, dict) else None
            if isinstance(tcalls_resp, list):
                for tc in tcalls_resp:
                    if isinstance(tc, dict):
                        args = (tc.get("function") or {}).get("arguments") or ""
                        if isinstance(args, str):
                            estimated_completion_chars += len(args)
        # Tool call detection: response.tool_calls is a list of dicts.
        resp = ai.get("response") or {}
        tcalls = resp.get("tool_calls") if isinstance(resp, dict) else None
        if not tcalls:
            continue
        for tc in tcalls:
            name = (tc.get("function") or {}).get("name") if isinstance(tc, dict) else None
            if not name:
                name = tc.get("name") if isinstance(tc, dict) else None
            if not name:
                continue
            tool_call_counts[name] = tool_call_counts.get(name, 0) + 1
            if name == "read_file":
                read_file_calls += 1
            elif name == "grep":
                grep_calls += 1
            elif name in GRAPH_RAG_TOOLS:
                graph_rag_calls += 1
            elif name == "ask_explorer":
                explorer_calls += 1
    # Apply char-based fallback when no native usage was reported.
    if total_toks == 0 and estimated_prompt_chars > 0:
        prompt_toks = estimated_prompt_chars // 4
        completion_toks = estimated_completion_chars // 4
        total_toks = prompt_toks + completion_toks

    final_status = ticket["status"]
    final_answer_reached = any(
        e.get("kind") in {"completed", "done", "final_answer"}
        for e in ticket_events
    )
    compile_pass = any(
        "compile_green" in (e.get("body") or "")
        or "BUILD SUCCESS" in (e.get("body") or "")
        for e in ticket_events
    )

    return {
        "final_status": final_status,
        "final_answer_reached": bool(final_answer_reached),
        "compile_pass": bool(compile_pass),
        "wall_clock_s": round(wall_clock_s, 1),
        "llm_call_count": llm_call_count,
        "llm_total_dur_ms": total_dur_ms,
        "prompt_tokens": prompt_toks,
        "completion_tokens": completion_toks,
        "total_tokens": total_toks or (prompt_toks + completion_toks),
        "tokens_estimated": estimated_prompt_chars > 0,
        "tool_call_distribution": dict(sorted(
            tool_call_counts.items(), key=lambda kv: -kv[1])),
        "read_file_calls": read_file_calls,
        "grep_calls": grep_calls,
        "graph_rag_calls": graph_rag_calls,
        "explorer_calls": explorer_calls,
    }


def _write_result(track: str, fixture_id: str, seq: int,
                  payload: dict) -> Path:
    out_dir = RESULTS_DIR / track / fixture_id
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"{seq:02d}_{ts}.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    return out_path


def run_one(track: str, fixture: dict, seq: int, api: str,
            timeout_s: int) -> dict:
    print(f"[track={track} fixture={fixture['id']} seq={seq}] starting...")
    with httpx.Client(base_url=api) as client:
        ticket = _post_ticket(client, fixture)
        identifier = ticket["identifier"]
        print(f"  ticket={identifier} title={ticket['title']!r}")
        t0 = time.time()
        try:
            final = _poll_until_done(client, identifier, timeout_s=timeout_s)
        except TimeoutError as exc:
            print(f"  TIMEOUT: {exc}")
            return {"error": str(exc), "ticket_identifier": identifier}
        wall_clock = time.time() - t0
        trace = _fetch_trace(client, identifier)
    metrics = _compute_metrics(trace, final["ticket"],
                               final.get("events") or [], wall_clock)
    payload = {
        "track": track,
        "fixture_id": fixture["id"],
        "fixture_name": fixture.get("name", ""),
        "ticket_identifier": identifier,
        "seq": seq,
        "metrics": metrics,
        "trace_event_count": len(trace),
    }
    out_path = _write_result(track, fixture["id"], seq, payload)
    print(f"  done → {out_path}")
    print(f"  metrics: status={metrics['final_status']} "
          f"compile={metrics['compile_pass']} "
          f"toks={metrics['total_tokens']} "
          f"steps={metrics['llm_call_count']} "
          f"wall={metrics['wall_clock_s']}s")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", required=True, choices=["Z", "A", "B"])
    ap.add_argument("--fixture", required=True, help="Fixture id (e.g. F1)")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--timeout", type=int, default=1200,
                    help="Per-run timeout seconds (default 1200 = 20 min)")
    args = ap.parse_args()

    fixture = _load_fixture(args.fixture)
    print(f"=== EVAL track={args.track} fixture={fixture['id']} "
          f"samples={args.samples} api={args.api} ===")
    for seq in range(1, args.samples + 1):
        try:
            run_one(args.track, fixture, seq, args.api, args.timeout)
        except KeyboardInterrupt:
            sys.exit("interrupted")
        except Exception as exc:
            print(f"  ERROR seq={seq}: {exc}")


if __name__ == "__main__":
    main()
