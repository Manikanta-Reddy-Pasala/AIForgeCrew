# ADK 2.0.0b1 sidecar

Parallel venv pinning `google-adk==2.0.0b1`. Runs alongside the
production 1.31.1 path; flip `AIFORGE_USE_ADK2=1` on a single role
to A/B inside one orchestrator process.

## Why isolated venv

ADK 2.0 makes breaking changes to:

- agent API (`Agent`, `BaseNode`, `Workflow(edges=...)`)
- event model (`Event(state=...)`, `RequestInput`)
- session schema — **incompatible with 1.x DB**

Pinning in a sidecar venv lets us iterate without poisoning the
prod aiforge env or touching the live Postgres `sessions` table.

## Bootstrap

```bash
python -m venv .venv-adk2
. .venv-adk2/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -c "import google.adk; print(google.adk.__version__)"
```

## Workflow port (skeleton)

```python
from google.adk import Agent, Workflow, Context, Event
from google.adk.workflow import node, RetryConfig
from google.adk.events import RequestInput

@node(retry_config=RetryConfig(max_attempts=3, initial_delay=2))
def planner(ctx: Context):
    yield Event(state={"plan": "..."})

@node
def doer(ctx: Context):
    yield Event(state={"edits": [...]})

@node
def feedback(ctx: Context):
    if ctx.state.get("compile_green"):
        return "done"
    return doer  # loop

root = Workflow(
    name="aiforge_v2",
    edges=[
        ("START", planner, doer),
        (doer, feedback),
        (feedback, doer),         # retry edge
        (feedback, "END"),
    ],
)
```

## Cut-over plan

Phase B (1-2 weeks):
1. Port `aiforge_core/runtime/adk_workflow.py` → `Workflow(BaseNode)`.
2. A/B run on isolated tickets, compare metrics vs 1.31.1 path.
3. Migrate session schema (one-shot SQL: `sessions` → `sessions_v2`).
4. Replace `ask_user` poll with `RequestInput` HITL.

Phase C (cut-over):
5. Drop 1.31.1 from prod venv.
6. Rip `legacy/` paths.

## Status

- [x] Sidecar venv directory + requirements pinned
- [ ] Workflow port (`workflow.py`)
- [ ] HITL `RequestInput` migration of `ask_user`
- [ ] Session schema migration script
- [ ] A/B comparison harness
