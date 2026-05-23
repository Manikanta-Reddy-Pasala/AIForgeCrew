#!/usr/bin/env python3
"""Async bench harness: oMLX vs LM Studio on OpenAI-compatible /v1/chat/completions.

Usage (run on the Mac Studio):

  python bench.py --server lmstudio --base http://localhost:1234/v1 --model qwen3-coder-30b-a3b-mlx --domain coder --phase A
  python bench.py --server omlx     --base http://localhost:8000/v1 --model qwen3-coder-30b-a3b-mlx --domain coder --phase A

Each invocation runs ONE (server, model, domain, phase) cell and writes a single JSON file
under results/<run_id>/<server>_<model>_<domain>_<phase>.json. Aggregation lives in run-remote.sh.

Streams via SSE to get accurate TTFT. Reports raw and per-token timings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

# Same-dir import; bench.py and prompts.py travel together.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompts import (
    CONCURRENT_BATCHES,
    MULTITURN_CONVOS,
    SINGLE_PROMPTS,
    context_for,
    system_for,
)

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)


async def stream_chat(
    client: httpx.AsyncClient,
    base: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float = 0.2,
) -> dict:
    """Stream a chat completion. Returns timing + token stats for one request."""
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    t_send = time.perf_counter()
    ttft: float | None = None
    out_chars = 0
    out_tokens_reported: int | None = None
    prompt_tokens_reported: int | None = None

    async with client.stream("POST", f"{base}/chat/completions", json=payload) as resp:
        resp.raise_for_status()
        async for raw in resp.aiter_lines():
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    if ttft is None:
                        ttft = time.perf_counter() - t_send
                    out_chars += len(piece)
            usage = obj.get("usage")
            if usage:
                prompt_tokens_reported = usage.get("prompt_tokens", prompt_tokens_reported)
                out_tokens_reported = usage.get("completion_tokens", out_tokens_reported)

    total = time.perf_counter() - t_send
    decode_time = max(total - (ttft or 0.0), 1e-6)
    out_toks = out_tokens_reported if out_tokens_reported is not None else max(out_chars // 4, 1)
    return {
        "ttft_s": ttft,
        "total_s": total,
        "decode_s": decode_time,
        "decode_tok_s": out_toks / decode_time if ttft is not None else None,
        "out_tokens": out_toks,
        "prompt_tokens": prompt_tokens_reported,
        "out_chars": out_chars,
    }


async def warmup(client: httpx.AsyncClient, base: str, model: str) -> None:
    """One throwaway request to compile/load + JIT first decode path."""
    try:
        await stream_chat(
            client,
            base,
            model,
            messages=[{"role": "user", "content": "Reply with the single word: ready."}],
            max_tokens=8,
        )
    except Exception as e:
        print(f"[warn] warmup failed: {e}", file=sys.stderr)


async def phase_a(client, base, model, domain, n=5) -> list[dict]:
    """Cold-style single prompts. We do NOT reset the server between samples (impractical),
    but we use distinct prompts so prefix cache cannot help us across samples."""
    sys_msg = system_for(domain)
    results = []
    for i, prompt in enumerate(SINGLE_PROMPTS[domain][:n]):
        messages = [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": prompt},
        ]
        r = await stream_chat(client, base, model, messages, max_tokens=512)
        r.update({"sample": i, "prompt_chars": len(prompt)})
        results.append(r)
        await asyncio.sleep(1.0)
    return results


async def phase_b(client, base, model, domain, convs=3) -> list[dict]:
    """Multi-turn shared-prefix. Each conversation: turn 1 is cold (no shared prefix in cache),
    turns 2-4 should hit the prefix cache if the server supports it."""
    sys_msg = system_for(domain)
    ctx = context_for(domain)
    results = []
    for ci, conv in enumerate(MULTITURN_CONVOS[domain][:convs]):
        history = [
            {"role": "system", "content": conv["system"]},
            {"role": "user", "content": conv["context"]},
            {"role": "assistant", "content": "Understood. Ask your question."},
        ]
        for ti, turn in enumerate(conv["turns"]):
            history.append({"role": "user", "content": turn})
            r = await stream_chat(client, base, model, history, max_tokens=256)
            r.update({"conv": ci, "turn": ti, "history_msgs": len(history)})
            results.append(r)
            # Append a short fake assistant turn to keep history compact and let the next
            # user turn pick up the cached prefix.
            history.append({"role": "assistant", "content": "(elided)"})
            await asyncio.sleep(0.5)
    return results


async def phase_c(client, base, model, domain, batches=3, concurrency=0) -> list[dict]:
    """Concurrent: fire requests in parallel. Measures continuous batching.

    concurrency=0 means use the natural batch size (4) from CONCURRENT_BATCHES.
    concurrency=N replaces each batch with N prompts cycled from the domain pool.
    """
    sys_msg = system_for(domain)
    results = []
    src_batches = CONCURRENT_BATCHES[domain][:batches]
    if concurrency and concurrency > 0:
        pool = [p for batch in CONCURRENT_BATCHES[domain] for p in batch]
        src_batches = [[pool[(i + b * concurrency) % len(pool)] for i in range(concurrency)]
                       for b in range(batches)]
    for bi, batch in enumerate(src_batches):

        async def one(p):
            messages = [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": p},
            ]
            return await stream_chat(client, base, model, messages, max_tokens=256)

        t_batch = time.perf_counter()
        per_req = await asyncio.gather(*(one(p) for p in batch))
        batch_wall = time.perf_counter() - t_batch
        for ri, r in enumerate(per_req):
            r.update({"batch": bi, "req": ri, "batch_wall_s": batch_wall, "batch_n": len(batch)})
            results.append(r)
        await asyncio.sleep(2.0)
    return results


def summarize(rows: list[dict], phase: str) -> dict:
    if not rows:
        return {}
    def s(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        if not vals:
            return None
        return {
            "min": min(vals),
            "p50": statistics.median(vals),
            "p95": statistics.quantiles(vals, n=20)[-1] if len(vals) >= 5 else max(vals),
            "max": max(vals),
            "mean": statistics.fmean(vals),
            "n": len(vals),
        }
    out = {
        "ttft_s": s("ttft_s"),
        "decode_tok_s": s("decode_tok_s"),
        "total_s": s("total_s"),
        "out_tokens": s("out_tokens"),
    }
    if phase == "C":
        batch_walls = sorted({r["batch_wall_s"] for r in rows if "batch_wall_s" in r})
        if batch_walls:
            agg_tok = sum(r["out_tokens"] for r in rows) / sum(batch_walls)
            out["aggregate_tok_s_across_batches"] = agg_tok
    if phase == "B":
        # Cache-reuse signal: turn-1 TTFT vs mean of turns 2-4.
        by_turn = {}
        for r in rows:
            by_turn.setdefault(r["turn"], []).append(r["ttft_s"])
        if 0 in by_turn and any(t in by_turn for t in (1, 2, 3)):
            t0 = statistics.fmean(by_turn[0])
            later = [v for t in (1, 2, 3) if t in by_turn for v in by_turn[t]]
            if later:
                out["ttft_turn0_mean_s"] = t0
                out["ttft_turn123_mean_s"] = statistics.fmean(later)
                out["cache_speedup_ratio"] = t0 / statistics.fmean(later) if later else None
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True, choices=["lmstudio", "omlx"])
    ap.add_argument("--base", required=True, help="e.g. http://localhost:1234/v1")
    ap.add_argument("--model", required=True)
    ap.add_argument("--domain", required=True, choices=["coder", "general"])
    ap.add_argument("--phase", required=True, choices=["A", "B", "C"])
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--skip-warmup", action="store_true")
    ap.add_argument("--concurrency", type=int, default=0,
                    help="Override phase C batch size (e.g. 8). 0 = use file default (4).")
    args = ap.parse_args()

    out_dir = Path(args.out_dir) / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{args.server}_{args.model.replace('/', '_')}_{args.domain}_{args.phase}.json"

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        if not args.skip_warmup:
            await warmup(client, args.base, args.model)

        if args.phase == "A":
            rows = await phase_a(client, args.base, args.model, args.domain)
        elif args.phase == "B":
            rows = await phase_b(client, args.base, args.model, args.domain)
        else:
            rows = await phase_c(client, args.base, args.model, args.domain,
                                 concurrency=args.concurrency)

    summary = summarize(rows, args.phase)
    payload = {
        "meta": {
            "server": args.server,
            "base": args.base,
            "model": args.model,
            "domain": args.domain,
            "phase": args.phase,
            "run_id": args.run_id,
            "host": os.uname().nodename,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
        "summary": summary,
        "rows": rows,
    }
    out_file.write_text(json.dumps(payload, indent=2))
    print(f"[ok] {out_file}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
