#!/usr/bin/env python3
"""DSPy A/B experiment — does prompt COMPILATION beat our hand-written
triage prompt on the local model? (https://dspy.ai)

Non-invasive dev overlay (nothing in the runtime changes):
    uv run --with dspy python scripts/dspy_experiment.py \
        --base-url http://127.0.0.1:1234/v1 --model <id>

Task under test: the TRIAGE complexity classifier (trivial|moderate|hard) —
single call, unambiguous ground truth, and it gates the fast-path routing,
so accuracy here has real routing consequences.

Three arms over a 30-ticket labeled set (15 train / 15 held-out eval):
  A. baseline  — our production prompt (prompts_extended.TRIAGE) via a raw
                 chat completion, JSON parsed like graph_pipeline does.
  B. dspy-zero — dspy.ChainOfThought over a typed Signature, no compilation.
  C. dspy-opt  — same module COMPILED with BootstrapFewShot on the train
                 half (few-shot demos selected by the optimizer).

Prints per-arm accuracy on the held-out half. If C consistently beats A,
the follow-up is a compiled-prompt export for the triage seam (adapter
pattern, env-gated) — NOT a dspy runtime dependency.
"""
from __future__ import annotations

import argparse
import json
import re

# (ticket_text, gold_complexity) — gold follows the production heuristics:
# trivial <=2 files mechanical; moderate 3-6 one subsystem; hard cross-cutting.
DATASET: list[tuple[str, str]] = [
    ("fix the typo in the README badge link", "trivial"),
    ("rename the variable retryCount to retry_count in sync_client.py", "trivial"),
    ("bump the version string in pyproject.toml to 1.2.0", "trivial"),
    ("delete the unused import in models/user.py", "trivial"),
    ("change the default page size from 20 to 50 in the config", "trivial"),
    ("update the copyright year in the footer template", "trivial"),
    ("fix the off-by-one in the pagination helper and add a unit test", "trivial"),
    ("correct the log level from info to debug in the scheduler", "trivial"),
    ("add an is_active flag to the Customer entity with migration, "
     "repository filter and tests", "moderate"),
    ("add a /health endpoint to the api with a smoke test", "moderate"),
    ("implement retry with exponential backoff in the sync client and "
     "cover it with tests", "moderate"),
    ("add csv export to the report module including unit tests", "moderate"),
    ("cache the category lookups with a ttl and invalidation on write", "moderate"),
    ("add input validation to the expense create endpoint and return 422 "
     "with field errors", "moderate"),
    ("support a --dry-run flag across the cli commands", "moderate"),
    ("add soft-delete to expenses: flag column, filtered queries, restore "
     "command and tests", "moderate"),
    ("extract the email sending into a service with an interface and add "
     "a fake for tests", "moderate"),
    ("add rate limiting middleware to the api with configurable buckets "
     "and tests", "moderate"),
    ("migrate the storage layer from sqlite to postgres across all "
     "repositories, update every query, migrations and the test "
     "fixtures", "hard"),
    ("introduce multi-tenancy: tenant id on every table, scoped queries, "
     "auth propagation, and migration of existing data", "hard"),
    ("split the monolith module into storage, domain and api packages "
     "with a compatibility layer", "hard"),
    ("replace the ad-hoc auth with oauth2 including refresh tokens, "
     "middleware, session revocation and docs", "hard"),
    ("redesign the report engine to stream aggregates incrementally "
     "instead of loading everything in memory", "hard"),
    ("make the pipeline event-sourced: append-only event log, projections, "
     "replay tooling", "hard"),
    ("add end-to-end encryption for stored attachments with key rotation", "hard"),
    ("port the cli from click to a plugin architecture with dynamic "
     "command discovery and back-compat", "hard"),
    ("unify the three duplicated validation layers into one schema-driven "
     "validator used by api, cli and importer", "hard"),
    ("internationalize the whole ui: extract strings, locale files, "
     "pluralization, and a language switcher", "hard"),
    ("add optimistic locking with version columns to every aggregate and "
     "handle conflicts in all writers", "hard"),
    ("upgrade the framework major version and fix all breaking api "
     "changes across the codebase", "hard"),
]


def _parse_verdict(out: str) -> str:
    m = re.search(r"\{.*\}", out or "", re.DOTALL)
    if m:
        try:
            c = str(json.loads(m.group(0)).get("complexity", "")).lower()
            if c in ("trivial", "moderate", "hard"):
                return c
        except ValueError:
            pass
    low = (out or "").lower()
    for c in ("trivial", "moderate", "hard"):
        if c in low:
            return c
    return "moderate"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="not-needed")
    args = ap.parse_args()

    train = DATASET[0::2]
    test = DATASET[1::2]

    # ── Arm A: production prompt via raw completion ─────────────────────
    from aiforge_core.runtime.prompts_extended.triage import PROMPT as TRIAGE
    import httpx

    def _complete(system: str, user: str) -> str:
        r = httpx.post(f"{args.base_url.rstrip('/')}/chat/completions",
                       json={"model": args.model, "temperature": 0,
                             "messages": [
                                 {"role": "system", "content": system},
                                 {"role": "user", "content": user}]},
                       headers={"Authorization": f"Bearer {args.api_key}"},
                       timeout=120)
        return r.json()["choices"][0]["message"]["content"]

    a_hits = 0
    for text, gold in test:
        a_hits += _parse_verdict(_complete(TRIAGE, text)) == gold
    print(f"A baseline (production prompt): {a_hits}/{len(test)}")

    # ── Arms B + C: dspy ────────────────────────────────────────────────
    import dspy

    lm = dspy.LM(f"openai/{args.model}", api_base=args.base_url,
                 api_key=args.api_key, temperature=0, max_tokens=300)
    dspy.configure(lm=lm)

    from typing import Literal

    class Triage(dspy.Signature):
        """Classify a dev ticket's implementation complexity. trivial =
        <=2 files mechanical edit; moderate = 3-6 files one subsystem;
        hard = cross-cutting / architectural / >6 files. Round UP when
        between tiers."""
        ticket: str = dspy.InputField()
        complexity: Literal["trivial", "moderate", "hard"] = dspy.OutputField()

    module = dspy.ChainOfThought(Triage)

    def _acc(mod) -> int:
        hits = 0
        for text, gold in test:
            try:
                hits += mod(ticket=text).complexity == gold
            except Exception as exc:  # noqa: BLE001
                print("  (miss on error:", str(exc)[:80], ")")
        return hits

    print(f"B dspy zero-shot:               {_acc(module)}/{len(test)}")

    trainset = [dspy.Example(ticket=t, complexity=g).with_inputs("ticket")
                for t, g in train]
    metric = lambda ex, pred, trace=None: ex.complexity == pred.complexity  # noqa: E731
    opt = dspy.BootstrapFewShot(metric=metric, max_bootstrapped_demos=4,
                                max_labeled_demos=4)
    compiled = opt.compile(module, trainset=trainset)
    print(f"C dspy compiled (BootstrapFewShot): {_acc(compiled)}/{len(test)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
