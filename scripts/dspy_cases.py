#!/usr/bin/env python3
"""DSPy use-case A/B suite — extract / agent / pipeline (dspy.ai cases)
measured against OUR production equivalents on the same local model.

Run (dev overlay, nothing in the runtime changes):
    uv run --with dspy python scripts/dspy_cases.py \
        --base-url http://127.0.0.1:1234/v1 --model <id> [--case all]

Cases:
  extract  — typed field extraction from ticket text.
             OURS: llm.structured.structured_complete (production seam).
             DSPY: typed Signature + Predict.
             Metric: exact field accuracy over 10 labeled tickets.
  agent    — tool-using agent, 6 tasks over two toy tools.
             OURS: our ACTION/ARGS_JSON text protocol + manual loop
             (the chat agent's protocol, miniaturized).
             DSPY: dspy.ReAct with the same tools.
             Metric: correct final numeric answer.
  pipeline — two-stage program: ask → spec → file plan.
             OURS: production _enhance + _architect.
             DSPY: two chained ChainOfThought modules.
             Metric: plans passing OUR deterministic _validate_plan
             (+ ≥2 files incl. a test file).
  (multi-model: N/A on a single local endpoint — per-role endpoints in
   agents.yaml already give the stack multi-model routing.)

NOTE: no `from __future__ import annotations` here — dspy Signatures
reject stringified type hints.
"""
import argparse
import json
import re
from typing import Literal

from pydantic import BaseModel

# ══════════════════ case: extract ══════════════════

EXTRACT_SET = [
    ("Login page throws 500 after the oauth change. Critical, blocks all "
     "users. auth/session.py and middleware.py involved. No schema change.",
     {"priority": "critical", "needs_migration": False}),
    ("Minor: tooltip typo on the settings page. ui only.",
     {"priority": "low", "needs_migration": False}),
    ("Add a 'last_seen' column to users and backfill it; medium urgency.",
     {"priority": "medium", "needs_migration": True}),
    ("High priority: payments webhook drops events under load; add retry "
     "queue table and consumer.",
     {"priority": "high", "needs_migration": True}),
    ("Cleanup: remove dead feature flags from config. Low.",
     {"priority": "low", "needs_migration": False}),
    ("Critical data loss: expenses table missing FK, add constraint + "
     "migration tonight.",
     {"priority": "critical", "needs_migration": True}),
    ("Medium: rename Customer.name to full_name across api and db.",
     {"priority": "medium", "needs_migration": True}),
    ("High: rate limiter misconfigured, tune limits in settings. No db.",
     {"priority": "high", "needs_migration": False}),
    ("Low priority doc-string cleanup across the storage module.",
     {"priority": "low", "needs_migration": False}),
    ("Add audit_log table and write hooks on every mutation. High.",
     {"priority": "high", "needs_migration": True}),
]


class TicketFacts(BaseModel):
    priority: Literal["low", "medium", "high", "critical"]
    needs_migration: bool


def case_extract(args) -> None:
    # OURS — the production structured seam (instructor or fallback loop).
    from aiforge_core.llm.structured import structured_complete
    ours = 0
    for text, gold in EXTRACT_SET:
        try:
            got = structured_complete("triage", [
                {"role": "system",
                 "content": "Extract facts from the dev ticket."},
                {"role": "user", "content": text}], TicketFacts,
                max_retries=1, max_tokens=200)
            ours += (got.priority == gold["priority"]
                     and got.needs_migration == gold["needs_migration"])
        except Exception as exc:  # noqa: BLE001
            print("  ours error:", str(exc)[:80])
    print(f"extract OURS (structured_complete): {ours}/{len(EXTRACT_SET)}")

    import dspy
    _configure_dspy(args)

    class Extract(dspy.Signature):
        """Extract facts from a dev ticket."""
        ticket: str = dspy.InputField()
        priority: Literal["low", "medium", "high", "critical"] = dspy.OutputField()
        needs_migration: bool = dspy.OutputField()

    mod = dspy.Predict(Extract)
    hits = 0
    for text, gold in EXTRACT_SET:
        try:
            p = mod(ticket=text)
            hits += (p.priority == gold["priority"]
                     and p.needs_migration == gold["needs_migration"])
        except Exception as exc:  # noqa: BLE001
            print("  dspy error:", str(exc)[:80])
    print(f"extract DSPY (typed Predict):        {hits}/{len(EXTRACT_SET)}")


# ══════════════════ case: agent ══════════════════

PRICES = {"apple": 2.0, "orange": 3.0, "banana": 1.5, "melon": 6.0,
          "coffee": 4.5, "tea": 2.5}

AGENT_TASKS = [
    ("what do 2 apples and 3 oranges cost in total?", 13.0),
    ("price of one melon plus one coffee?", 10.5),
    ("i buy 4 bananas — total?", 6.0),
    ("two teas and one apple, total cost?", 7.0),
    ("a coffee and a banana together?", 6.0),
    ("3 melons total price?", 18.0),
]


def lookup_price(item: str) -> float:
    """Unit price of a grocery item."""
    return PRICES[item.strip().lower().rstrip("s")]


def add(a: float, b: float) -> float:
    """Sum two numbers."""
    return float(a) + float(b)


_OUR_AGENT_SYS = """You solve tasks with tools, one step per turn.
Tools:
- lookup_price {"item": "<name>"}   (unit price of one item)
- add {"a": <num>, "b": <num>}      (sum two numbers)
Reply with EXACTLY one of:
ACTION: <tool>
ARGS_JSON: {...}
or, when you know the final numeric answer:
FINAL: <number>
No other text."""


def _our_mini_agent(complete, task: str) -> float | None:
    convo = [{"role": "system", "content": _OUR_AGENT_SYS},
             {"role": "user", "content": task}]
    for _ in range(10):
        out = complete(convo)
        m = re.search(r"FINAL:\s*([-\d.]+)", out or "")
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        am = re.search(r"ACTION:\s*(\w+)\s*\nARGS_JSON:\s*(\{.*?\})",
                       out or "", re.DOTALL)
        convo.append({"role": "assistant", "content": out or ""})
        if not am:
            convo.append({"role": "user", "content":
                          "Reply with ACTION/ARGS_JSON or FINAL only."})
            continue
        name, raw = am.group(1), am.group(2)
        try:
            a = json.loads(raw)
            res = (lookup_price(**a) if name == "lookup_price"
                   else add(**a) if name == "add" else f"unknown tool {name}")
        except Exception as exc:  # noqa: BLE001
            res = f"error: {exc}"
        convo.append({"role": "user", "content": f"OBSERVATION: {res}"})
    return None


def case_agent(args) -> None:
    import httpx

    def complete(convo):
        r = httpx.post(f"{args.base_url.rstrip('/')}/chat/completions",
                       json={"model": args.model, "temperature": 0,
                             "messages": convo},
                       headers={"Authorization": f"Bearer {args.api_key}"},
                       timeout=120)
        return r.json()["choices"][0]["message"]["content"]

    ours = 0
    for task, gold in AGENT_TASKS:
        got = _our_mini_agent(complete, task)
        ours += got is not None and abs(got - gold) < 0.01
    print(f"agent OURS (text protocol loop): {ours}/{len(AGENT_TASKS)}")

    import dspy
    _configure_dspy(args)

    class Solve(dspy.Signature):
        """Answer the grocery-cost question using the tools."""
        question: str = dspy.InputField()
        answer: float = dspy.OutputField()

    react = dspy.ReAct(Solve, tools=[lookup_price, add], max_iters=8)
    hits = 0
    for task, gold in AGENT_TASKS:
        try:
            got = react(question=task).answer
            hits += got is not None and abs(float(got) - gold) < 0.01
        except Exception as exc:  # noqa: BLE001
            print("  dspy error:", str(exc)[:80])
    print(f"agent DSPY (dspy.ReAct):         {hits}/{len(AGENT_TASKS)}")


# ══════════════════ case: pipeline ══════════════════

PIPELINE_ASKS = [
    "build a url shortener api with sqlite storage and unit tests",
    "build a markdown note-taking cli with tagging and search, tested",
    "build a csv budget analyzer module with monthly summaries and tests",
    "build a webhook relay service with retry queue and tests",
    "build a password vault cli: encrypt store, list, get commands, tests",
    "build an rss digest generator emailing daily summaries, with tests",
]


def _plan_ok(files: list[dict]) -> bool:
    from aiforge_core.runtime.parallel_subtasks import _validate_plan
    clean, issues = _validate_plan(files)
    return len(clean) >= 2 and not issues


def case_pipeline(args) -> None:
    from aiforge_core.runtime import parallel_subtasks as pp
    ours = 0
    for ask in PIPELINE_ASKS:
        try:
            spec = pp._enhance(ask)
            files = pp._architect(spec, cwd=None)
            ours += _plan_ok(files)
        except Exception as exc:  # noqa: BLE001
            print("  ours error:", str(exc)[:80])
    print(f"pipeline OURS (enhance→architect): {ours}/{len(PIPELINE_ASKS)}")

    import dspy
    _configure_dspy(args)

    class Spec(dspy.Signature):
        """Rewrite the ask into a concrete build spec: goal, modules, tests."""
        ask: str = dspy.InputField()
        spec: str = dspy.OutputField()

    class Plan(dspy.Signature):
        """Design the file plan for the spec: disjoint files, a test file per
        code module. JSON list of {path, purpose}."""
        spec: str = dspy.InputField()
        files_json: str = dspy.OutputField(desc='[{"path": "...", "purpose": "..."}]')

    stage1, stage2 = dspy.ChainOfThought(Spec), dspy.ChainOfThought(Plan)
    hits = 0
    for ask in PIPELINE_ASKS:
        try:
            spec = stage1(ask=ask).spec
            raw = stage2(spec=spec).files_json
            m = re.search(r"\[.*\]", raw or "", re.DOTALL)
            files = [{"path": f.get("path", ""), "purpose": f.get("purpose", ""),
                      "api": []} for f in (json.loads(m.group(0)) if m else [])]
            hits += _plan_ok(files)
        except Exception as exc:  # noqa: BLE001
            print("  dspy error:", str(exc)[:80])
    print(f"pipeline DSPY (2×ChainOfThought):  {hits}/{len(PIPELINE_ASKS)}")


# ══════════════════ main ══════════════════

def _configure_dspy(args) -> None:
    import dspy
    dspy.configure(lm=dspy.LM(f"openai/{args.model}", api_base=args.base_url,
                              api_key=args.api_key, temperature=0,
                              max_tokens=2000))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="not-needed")
    ap.add_argument("--case", default="all",
                    choices=["all", "extract", "agent", "pipeline"])
    args = ap.parse_args()
    for name, fn in (("extract", case_extract), ("agent", case_agent),
                     ("pipeline", case_pipeline)):
        if args.case in ("all", name):
            print(f"── case: {name} ──")
            fn(args)
    print("(multi-model: N/A on one local endpoint — per-role endpoints in "
          "agents.yaml already provide multi-model routing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
