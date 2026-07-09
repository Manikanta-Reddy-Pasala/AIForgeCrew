#!/usr/bin/env python3
"""Score the memory RAG pipeline with ragas (optional extra ``evals``).

For each question: recall contexts via unified_query, answer with the chat
role over those contexts, then judge with ragas metrics (faithfulness,
answer relevancy, + context precision when ground truths are given) using
the SAME local endpoint as the judge — fully local, no cloud.

Usage (ragas is a dev-tool OVERLAY, not a project extra — its langchain pins
conflict with aider-chat in one resolution universe):
  uv run --with 'ragas<0.4' --with 'langchain-openai<1' \
      python scripts/rag_eval.py --repo AIForgeCrew \
      --questions eval/rag_questions.jsonl [--limit 8]

Questions file: one JSON object per line — {"question": "...",
"ground_truth": "..."} (ground_truth optional). Without --questions a tiny
built-in smoke set is used.
"""
from __future__ import annotations

import argparse
import json
import sys


def _samples(args) -> list[dict]:
    if args.questions:
        rows = []
        with open(args.questions, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    rows.append(json.loads(ln))
        return rows[: args.limit] if args.limit else rows
    return [
        {"question": "How does push sync get data from the client to the server?"},
        {"question": "What happens when a workflow script fails its syntax check?"},
        {"question": "Where are Jira ticket attachments stored?"},
    ][: args.limit or 3]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=None, help="repo key for recall scoping")
    ap.add_argument("--questions", default=None, help="jsonl questions file")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--role", default="chat",
                    help="role whose endpoint answers AND judges (default chat)")
    args = ap.parse_args()

    from aiforge_core.integrations import ragas_adapter
    if not ragas_adapter.available():
        print("ragas not installed — run via the overlay:\n"
              "  uv run --with 'ragas<0.4' --with 'langchain-openai<1' "
              "python scripts/rag_eval.py", file=sys.stderr)
        return 2

    from aiforge_core.llm import client
    from aiforge_core.memory import unified_query

    ep = client.resolve(args.role)
    samples: list[dict] = []
    for row in _samples(args):
        q = row["question"]
        res = unified_query.query(q, limit=6, repo=args.repo)
        contexts = [h.get("text") or "" for h in (res.get("hits") or []) if h]
        contexts = [c for c in contexts if c.strip()][:6]
        answer = client.complete(args.role, [
            {"role": "system",
             "content": "Answer ONLY from the provided context. Be concise."},
            {"role": "user",
             "content": "Context:\n" + "\n---\n".join(contexts)
                        + f"\n\nQuestion: {q}"}], max_tokens=400)
        s = {"question": q, "contexts": contexts, "answer": answer or ""}
        if row.get("ground_truth"):
            s["ground_truth"] = row["ground_truth"]
        samples.append(s)
        print(f"· recalled {len(contexts)} contexts for: {q[:70]}")

    scores = ragas_adapter.evaluate_recall(
        samples, base_url=ep.base_url, api_key=ep.api_key, model=ep.model)
    print("\nRAG scores (avg over "
          f"{len(samples)} samples, judge={ep.model}):")
    for k, v in scores.items():
        print(f"  {k:24s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
