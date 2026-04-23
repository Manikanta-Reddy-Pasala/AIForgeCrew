#!/usr/bin/env python3
"""Ops Mongo agent over the OneShell POS MongoDB (read-only by convention).

EVAL-5 (2026-04-23). smolagents CodeAgent + two tools:
- ``mongo_find``: filter + projection + limit
- ``mongo_aggregate``: aggregation pipeline

Schema doc auto-derived by sampling one document per collection. qwen3.6-35b-a3b
is fine at translating natural-language questions into pymongo filters or
aggregation pipelines.

Usage:
    scripts/mongo_agent.py "how many products in business X have qty > 0?"
    scripts/mongo_agent.py --db oneshell --collections productTxn,sales \\
        --model qwen3.6-35b-a3b "<question>"

Connection:
    AIFORGE_MONGO_URI (default connects to localhost:27017 — start a
    kubectl port-forward first, e.g.
    ``kubectl port-forward svc/prod-cluster-mongos 27017:27017 \\
        -n mongodb --insecure-skip-tls-verify &``).

Safety:
- ``mongo_find`` is pure read.
- ``mongo_aggregate`` blocks pipelines containing ``$out``/``$merge`` stages.
- Use a dedicated read-only Mongo user in production.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys

from bson import ObjectId, json_util
from pymongo import MongoClient
from smolagents import CodeAgent, LiteLLMModel, tool

DEFAULT_URI = "mongodb://127.0.0.1:27017/oneshell"
MONGO_URI = os.environ.get("AIFORGE_MONGO_URI", DEFAULT_URI)
LM_BASE = os.environ.get("LM_BASE", "http://127.0.0.1:1234/v1")
LM_KEY = os.environ.get("LM_KEY", "lm-studio")

DEFAULT_COLLECTIONS = [
    "businessProducts", "productTxn", "sales", "saleOrder",
    "salesQuotation", "Parties", "allTransactions", "chartOfAccounts",
]


def _stringify(doc) -> str:
    # json_util handles ObjectId / Decimal128 / datetime cleanly.
    return json.dumps(doc, default=json_util.default, ensure_ascii=False)


def introspect_schema(client: MongoClient, db_name: str, collections: list[str]) -> str:
    db = client[db_name]
    lines = [f"Database: {db_name} (MongoDB, read-only by convention)", ""]
    for c in collections:
        try:
            doc = db[c].find_one(sort=[("_id", -1)])
        except Exception as exc:
            lines.append(f"-- {c}: error sampling ({exc})")
            continue
        if doc is None:
            lines.append(f"-- {c}: empty collection")
            continue
        shape = {k: type(v).__name__ for k, v in doc.items()}
        lines.append(f"{c}:")
        for k, tname in list(shape.items())[:20]:
            lines.append(f"  - {k}: {tname}")
    lines.append("")
    lines.append(
        "Notes:\n"
        "- Use `mongo_find(collection, filter_json, projection_json, limit)` for queries.\n"
        "- Use `mongo_aggregate(collection, pipeline_json)` for grouping/counting.\n"
        "- Pass filter and pipeline as JSON strings.\n"
        "- ObjectId appears as {'$oid': '...'} in samples."
    )
    return "\n".join(lines)


def make_tools(client: MongoClient, db_name: str):
    db = client[db_name]

    @tool
    def mongo_find(collection: str, filter_json: str = "{}",
                   projection_json: str = "{}",
                   limit: int = 50) -> str:
        """Run a read-only find query on a collection.

        Args:
            collection: Target collection name.
            filter_json: JSON string of the find filter (default ``{}``).
            projection_json: JSON string of the projection (default ``{}``).
            limit: Max documents to return (clamped to 200).
        """
        try:
            flt = json.loads(filter_json or "{}")
            proj = json.loads(projection_json or "{}")
        except Exception as exc:
            return f"ERROR: bad JSON: {exc}"
        n = max(1, min(int(limit or 50), 200))
        try:
            cursor = db[collection].find(flt, proj).limit(n)
            docs = list(cursor)
        except Exception as exc:
            return f"ERROR: {type(exc).__name__}: {exc}"
        if not docs:
            return "(no documents)"
        out = [_stringify(d) for d in docs]
        return "\n".join(out)

    @tool
    def mongo_aggregate(collection: str, pipeline_json: str) -> str:
        """Run a read-only aggregation pipeline.

        Args:
            collection: Target collection name.
            pipeline_json: JSON string of the aggregation pipeline (list).
        """
        try:
            pipeline = json.loads(pipeline_json)
        except Exception as exc:
            return f"ERROR: bad JSON pipeline: {exc}"
        if not isinstance(pipeline, list):
            return "ERROR: pipeline must be a JSON array"
        for stage in pipeline:
            if isinstance(stage, dict):
                if "$out" in stage or "$merge" in stage:
                    return "ERROR: $out / $merge stages blocked (write-capable)"
        try:
            cursor = db[collection].aggregate(pipeline, allowDiskUse=True)
            docs = list(cursor)
        except Exception as exc:
            return f"ERROR: {type(exc).__name__}: {exc}"
        if not docs:
            return "(no results)"
        return "\n".join(_stringify(d) for d in docs[:100])

    @tool
    def list_collections() -> str:
        """List collection names in the database.

        Returns a newline-separated string. The first line is ``count=N`` so
        "how many collections" questions can be answered by reading that
        header directly — do NOT call ``len()`` on the returned string.
        """
        try:
            names = sorted(db.list_collection_names())
            return f"count={len(names)}\n" + "\n".join(names)
        except Exception as exc:
            return f"ERROR: {exc}"

    return [mongo_find, mongo_aggregate, list_collections]


def build_agent(model_id: str, tools: list) -> CodeAgent:
    _lm_params = set(inspect.signature(LiteLLMModel.__init__).parameters)
    key = "model_id" if "model_id" in _lm_params else "model"
    mid = model_id if "/" in model_id else f"openai/{model_id}"
    model = LiteLLMModel(**{key: mid, "api_base": LM_BASE, "api_key": LM_KEY})
    return CodeAgent(
        tools=tools, model=model, max_steps=8,
        additional_authorized_imports=["re", "json"],
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("question", nargs="+")
    ap.add_argument("--model", default="qwen3.6-35b-a3b")
    ap.add_argument("--db", default=os.environ.get("AIFORGE_MONGO_DB", "oneshell"))
    ap.add_argument("--collections", default=",".join(DEFAULT_COLLECTIONS))
    args = ap.parse_args()

    collections = [c.strip() for c in args.collections.split(",") if c.strip()]
    question = " ".join(args.question).strip()

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")

    schema_doc = introspect_schema(client, args.db, collections)
    tools = make_tools(client, args.db)
    agent = build_agent(args.model, tools)

    preamble = (
        "You are an aiforge Mongo ops assistant. Answer by calling the "
        "`mongo_find`, `mongo_aggregate`, or `list_collections` tools. "
        "Pass filter/pipeline as JSON strings. Do NOT invent schemas — "
        "consult the schema doc. When you have the answer call "
        "`final_answer(<value>)`. Be terse.\n\n"
        + schema_doc
        + "\n\nQuestion: "
    )
    print(agent.run(preamble + question))
    return 0


if __name__ == "__main__":
    sys.exit(main())
