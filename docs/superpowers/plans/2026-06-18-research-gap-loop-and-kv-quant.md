# Research-Gap Loop + LM Studio KV-Quant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **UPDATE 2026-06-19 — Part B (KV-quant) DISCARDED.** Live check on Mac
> Studio: `lms load` has no KV-quant flag (`error: unknown option`), no
> persisted KV-quant config, no standalone `mlx_lm`. KV-quant is not
> achievable on the LM Studio runtime without migrating serving to
> `mlx_lm.server`, which was deferred. All Part-B code/docs were reverted;
> only **Part A (research-gap loop)** shipped and is live-validated.

**Goal:** (A) Add a bounded research-completeness loop to the v6 ADK `Workflow` pipeline — an LLM gap-evaluator re-dispatches the context fan-out once when research is judged insufficient. ~~(B) Wire LM Studio KV-cache quantization.~~ *(B discarded — see note above.)*

**Architecture:** Part A inserts `research_entry` (passthrough fan-out source) + `gap_eval` (tool-less JSON critic) + `gap_gate` (bounded router) between `merge_context` and `planner`; the gap edge re-enters `research_entry` so the whole context fan-out re-fires in one scheduler wave (required for `JoinNode` re-arm). Part B extends `local_starter._load_cmd` + a per-model manifest, gated by `AIFORGE_LMS_KV_BITS`, excluding vision/embedding models (KV-quant breaks MLX vision — obs-28582).

**Tech Stack:** Python 3.11+, `google.adk.workflow` (Workflow/Edge/JoinNode/node), pytest, LM Studio `lms` CLI over SSH.

**Specs:** `docs/superpowers/specs/2026-06-18-research-gap-loop-design.md`, `docs/superpowers/specs/2026-06-18-kv-cache-quant-design.md`

---

## PART A — Research-Gap Loop

### Task A1: Routing constants + `gap_gate` in `graph_pipeline.py`

**Files:**
- Modify: `aiforge_core/runtime/graph_pipeline.py`
- Test: `tests/runtime/test_gap_gate.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_gap_gate.py
import asyncio
import pytest
from aiforge_core.runtime import graph_pipeline as gp


class _Ctx:
    """Minimal stand-in for the ADK workflow Context: dict state + route."""
    def __init__(self, state):
        self.state = state
        self.route = None


def _run(node_body, state):
    ctx = _Ctx(state)
    asyncio.get_event_loop().run_until_complete(node_body(ctx))
    return ctx


def test_gap_gate_routes_research_gap_when_insufficient():
    ctx = _run(gp._gap_gate, {"gap_verdict": {"sufficient": False,
                                              "missing": ["token refresh path"],
                                              "queries": ["where is refresh"]}})
    assert ctx.route == gp.ROUTE_RESEARCH_GAP
    assert ctx.state["gap_pass_count"] == 1
    assert "token refresh path" in ctx.state["research_gap_brief_md"]


def test_gap_gate_routes_ok_when_sufficient():
    ctx = _run(gp._gap_gate, {"gap_verdict": {"sufficient": True}})
    assert ctx.route == gp.ROUTE_RESEARCH_OK


def test_gap_gate_caps_at_one_pass():
    ctx = _run(gp._gap_gate, {"gap_verdict": {"sufficient": False},
                              "gap_pass_count": 1})
    assert ctx.route == gp.ROUTE_RESEARCH_OK  # cap reached


def test_gap_gate_parse_failure_defaults_ok():
    ctx = _run(gp._gap_gate, {"gap_verdict": "not json at all"})
    assert ctx.route == gp.ROUTE_RESEARCH_OK  # never block on a critic slip
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/runtime/test_gap_gate.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_gap_gate'`

- [ ] **Step 3: Add constants + helpers + gate to `graph_pipeline.py`**

After the existing `MAX_VERIFY_REPLANS = 1` line, add:

```python
# Research-gap → re-search cap (bounded research-completeness loop).
MAX_GAP_PASSES = 1
ROUTE_RESEARCH_GAP = "research_gap"
ROUTE_RESEARCH_OK = "research_ok"
```

After `_parse_verdict` (before `_validator_failed`), add:

```python
def _gap_sufficient(raw: Any) -> bool:
    """True when the gap-evaluator judged research sufficient.

    Tolerant: a dict with ``sufficient`` wins; a JSON string is parsed;
    anything unparseable defaults to True so a critic formatting slip
    never traps the pipeline in a re-search loop (mirrors
    parallel_stages._coerce_verdict's fail-open stance)."""
    try:
        obj: Any = raw
        if isinstance(raw, str) and raw.strip():
            text = raw.strip().strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
            obj = json.loads(text)
        if isinstance(obj, dict) and "sufficient" in obj:
            return bool(obj["sufficient"])
    except Exception:
        pass
    return True


def _render_gap_brief(raw: Any) -> str:
    """Render the gap-evaluator's missing/queries into a researcher hint."""
    missing: list = []
    queries: list = []
    try:
        obj: Any = raw
        if isinstance(raw, str):
            text = raw.strip().strip("`")
            if text[:4].lower() == "json":
                text = text[4:]
            obj = json.loads(text)
        if isinstance(obj, dict):
            missing = [str(m) for m in (obj.get("missing") or []) if m]
            queries = [str(q) for q in (obj.get("queries") or []) if q]
    except Exception:
        pass
    lines = ["A prior research pass was judged INCOMPLETE. Specifically "
             "locate the following before the Planner runs:"]
    for m in missing:
        lines.append(f"  - MISSING: {m}")
    for q in queries:
        lines.append(f"  - SEARCH: {q}")
    return "\n".join(lines)
```

After `_verifier_gate` (before `_plan_promote`), add:

```python
async def _gap_gate(ctx):  # type: ignore[no-untyped-def]
    """Bounded research-completeness loop. If the gap-evaluator judged
    research insufficient and we have budget, re-dispatch the context
    fan-out (route research_gap → research_entry) with a targeted hint;
    otherwise proceed to the Planner."""
    state = ctx.state
    passes = int(state.get("gap_pass_count", 0) or 0)
    if not _gap_sufficient(state.get("gap_verdict")) and passes < MAX_GAP_PASSES:
        state["gap_pass_count"] = passes + 1
        state["research_gap_brief_md"] = _render_gap_brief(state.get("gap_verdict"))
        ctx.route = ROUTE_RESEARCH_GAP
        _trace(":ResearchGap", {"pass": passes + 1})
    else:
        ctx.route = ROUTE_RESEARCH_OK
```

Add the factory next to `make_verifier_gate`:

```python
def make_gap_gate():
    from google.adk.workflow import node
    return node(_gap_gate, name="gap_gate")
```

Extend `__all__`: add `"MAX_GAP_PASSES"`, `"ROUTE_RESEARCH_GAP"`, `"ROUTE_RESEARCH_OK"`, `"make_gap_gate"`, `"_gap_gate"`, `"_gap_sufficient"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/runtime/test_gap_gate.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/graph_pipeline.py tests/runtime/test_gap_gate.py
git commit -m "feat(pipeline): add bounded research-gap gate + routing constants"
```

---

### Task A2: `gap_eval` prompt module

**Files:**
- Create: `aiforge_core/runtime/prompts/gap_eval.py`
- Modify: `aiforge_core/runtime/prompts/__init__.py`
- Test: `tests/runtime/test_gap_eval_prompt.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_gap_eval_prompt.py
from aiforge_core.runtime import prompts


def test_gap_eval_prompt_exported_and_shaped():
    p = prompts.GAP_EVAL
    assert "sufficient" in p          # the JSON contract field
    assert "{context_brief_md?}" in p  # reads the merged research brief
    assert "{enhanced_body?}" in p     # reads the ticket
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/runtime/test_gap_eval_prompt.py -v`
Expected: FAIL — `AttributeError: module 'aiforge_core.runtime.prompts' has no attribute 'GAP_EVAL'`

- [ ] **Step 3: Create the prompt module**

```python
# aiforge_core/runtime/prompts/gap_eval.py
"""gap_eval prompt — research-completeness critic.

Runs AFTER merge_context, BEFORE the Planner. Judges whether the
assembled research brief is sufficient for the Planner to write a
grounded plan. Single-turn, no tools, strict JSON. Drives the bounded
research-gap loop (graph_pipeline._gap_gate).
"""
from __future__ import annotations

PROMPT = (
    "You are the Research-Completeness Critic. The Planner runs next and "
    "will plan ONLY from the research brief below. Judge whether that "
    "brief gives the Planner enough grounded context to write a correct "
    "plan for the ticket.\n"
    "\n"
    "Judge INSUFFICIENT (sufficient=false) if ANY hold:\n"
    "  - an acceptance criterion has no relevant file identified\n"
    "  - the brief names a behaviour but not where it lives in the code\n"
    "  - a clearly-required collaborator/config/test target is absent\n"
    "Otherwise judge sufficient=true. Bias toward true — a re-search is "
    "expensive; only flag a CONCRETE, nameable gap.\n"
    "\n"
    "Return STRICT JSON only:\n"
    '  {"sufficient": true|false, '
    '"missing": [<short phrase naming each absent thing>], '
    '"queries": [<a search phrase to find each missing thing>]}\n'
    "When sufficient=true, missing and queries MUST be empty arrays.\n"
    "\n"
    "--- Ticket (from pipeline state) ---\n"
    "{enhanced_body?}\n"
    "\n"
    "--- Assembled research brief to judge (state['context_brief_md']) ---\n"
    "{context_brief_md?}"
)

__all__ = ["PROMPT"]
```

In `aiforge_core/runtime/prompts/__init__.py`, add the import (after the `verify_risk` import line):

```python
from .gap_eval import PROMPT as GAP_EVAL
```

and add `"GAP_EVAL"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/runtime/test_gap_eval_prompt.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/prompts/gap_eval.py aiforge_core/runtime/prompts/__init__.py tests/runtime/test_gap_eval_prompt.py
git commit -m "feat(prompts): add gap_eval research-completeness critic prompt"
```

---

### Task A3: Researcher prompt gains the gap-hint block

**Files:**
- Modify: `aiforge_core/runtime/prompts_extended/researcher.py`
- Test: `tests/runtime/test_researcher_gap_block.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_researcher_gap_block.py
from aiforge_core.runtime import prompts_extended


def test_researcher_prompt_has_gap_block():
    assert "{research_gap_brief_md?}" in prompts_extended.RESEARCHER
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/runtime/test_researcher_gap_block.py -v`
Expected: FAIL — assertion error (block absent)

- [ ] **Step 3: Append the optional gap block to the researcher prompt**

In `aiforge_core/runtime/prompts_extended/researcher.py`, change the
final string segment from:

```python
    "--- Enhanced ticket (from pipeline state) ---\n"
    "{enhanced_body?}"
)
```

to:

```python
    "--- Enhanced ticket (from pipeline state) ---\n"
    "{enhanced_body?}\n"
    "\n"
    "{research_gap_brief_md?}"
)
```

(On the first pass `research_gap_brief_md` is absent — ADK `{x?}`
templating renders it empty. On a gap re-dispatch it carries the
targeted re-search hint.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/runtime/test_researcher_gap_block.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/prompts_extended/researcher.py tests/runtime/test_researcher_gap_block.py
git commit -m "feat(prompts): researcher consumes research_gap_brief_md hint on re-search"
```

---

### Task A4: `gap_eval` agent module

**Files:**
- Create: `aiforge_core/agents/gap_eval.py`
- Test: covered by Task A5's contract test (agent build needs the YAML entry first)

- [ ] **Step 1: Create the agent module (mirrors `verify_correctness.py`)**

```python
# aiforge_core/agents/gap_eval.py
"""gap_eval archetype — research-completeness critic.

Runs AFTER merge_context, BEFORE the Planner. Tool-less single-turn
JSON critic; writes ``gap_verdict``. The graph's ``gap_gate`` reads it
to decide whether to re-dispatch the context fan-out (bounded once).
"""
from __future__ import annotations

from aiforge_core.runtime import prompts

from . import _base

ROLE = "gap_eval"
PROMPT = prompts.GAP_EVAL
OUTPUT_KEY = "gap_verdict"
TOOLS_FACTORY = None


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
```

- [ ] **Step 2: Commit (no standalone test — exercised in A5/A7)**

```bash
git add aiforge_core/agents/gap_eval.py
git commit -m "feat(agents): add gap_eval archetype module"
```

---

### Task A5: Register `gap_eval` in agents.yaml + agent_config

**Files:**
- Modify: `aiforge_core/agents/agents.yaml`
- Modify: `aiforge_core/config/agent_config.py:36-49` (`_ARCHETYPES`)
- Test: `tests/agents/test_gap_eval_contract.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/agents/test_gap_eval_contract.py
from aiforge_core.agents import _base
from aiforge_core.config import agent_config


def test_gap_eval_in_archetypes():
    assert "gap_eval" in agent_config.list_roles()


def test_gap_eval_contract_loads():
    c = _base.contract_for("gap_eval")          # raises KeyError if missing
    assert c.contract.max_wall_s > 0
    assert "file_write" in (c.tools.forbidden or [])  # read-only judge
```

(`agent_config.list_roles()` returns `list(_ARCHETYPES)` — see
`agent_config.py:600`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/agents/test_gap_eval_contract.py -v`
Expected: FAIL — `gap_eval` not in archetypes / `KeyError` from `contract_for`

- [ ] **Step 3a: Add `gap_eval` to `_ARCHETYPES`**

In `aiforge_core/config/agent_config.py`, change the `verify_*` line in
`_ARCHETYPES` from:

```python
    "verify_correctness", "verify_scope", "verify_risk",
)
```

to:

```python
    "verify_correctness", "verify_scope", "verify_risk",
    # Research-completeness critic (2026-06-18) — drives the bounded
    # research-gap re-search loop. Local-tier, tool-less.
    "gap_eval",
)
```

- [ ] **Step 3b: Add the `gap_eval` entry to `agents.yaml`**

Insert after the `researcher:` block (mirror its local identity; it is a
tool-less judge so `allowed` is empty and `forbidden` is ALL):

```yaml
  gap_eval:
    identity:
      runtime: adk_agent_direct_litellm
      model: Qwen3.6-27B-MLX-4bit
      backend: direct_litellm
      base_url: http://127.0.0.1:1235/v1
      ctx_window: 64000
    contract:
      inputs:
        - enhanced_body
        - context_brief_md
      outputs:
        - gap_verdict
      max_turns: 1
      max_wall_s: 180
    tools:
      allowed: []
      forbidden:
        - file_write
        - file_patch
        - file_read
        - bash
        - run_shell
        - run_compile
        - code_run
        - ask_user
    memory:
      read_scope: full
      write_scope: none
    rule: >
      gap_eval is a tool-less research-completeness critic running
      PRE-planner (after merge_context). It writes only gap_verdict
      JSON ({sufficient, missing, queries}); the graph's gap_gate
      re-dispatches the context fan-out at most once when insufficient.
```

(If the loader rejects `forbidden` with a non-empty list when `allowed`
is empty — it does not; `forbidden_is_all` is only triggered by the
literal `ALL` token, see `loader.py:243` — keep the explicit list.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/agents/test_gap_eval_contract.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/agents/agents.yaml aiforge_core/config/agent_config.py tests/agents/test_gap_eval_contract.py
git commit -m "feat(config): register gap_eval archetype + contract"
```

---

### Task A6: `research_entry` passthrough node in `parallel_stages.py`

**Files:**
- Modify: `aiforge_core/runtime/parallel_stages.py`
- Test: `tests/runtime/test_research_entry.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_research_entry.py
import asyncio
from aiforge_core.runtime import parallel_stages as ps


class _Ctx:
    def __init__(self, state):
        self.state = state
        self.route = None


def test_research_entry_is_noop_passthrough():
    ctx = _Ctx({"enhanced_body": "x"})
    asyncio.get_event_loop().run_until_complete(ps.research_entry(ctx))
    assert ctx.state["enhanced_body"] == "x"   # state untouched


def test_make_research_entry_node_named():
    n = ps.make_research_entry_node()
    assert n.name == "research_entry"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/runtime/test_research_entry.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'research_entry'`

- [ ] **Step 3: Add the passthrough node + factory**

In `aiforge_core/runtime/parallel_stages.py`, after `merge_verdicts`,
add:

```python
async def research_entry(ctx):  # type: ignore[no-untyped-def]
    """No-op fan-out source for the context branches.

    Exists so the context fan-out has a single stable re-entry point:
    the first pass enters from the Enhancer, the research-gap loop
    re-enters here. Re-entering one node re-fires ALL outgoing branch
    edges in one scheduler wave, which is what context_join needs to
    re-arm (a JoinNode fired with only a subset of its in-branches
    rescheduled reads stale COMPLETED status — the ONE-117 max_concurrency
    note in pipeline.py). Body intentionally does nothing."""
    return None
```

In the `make_*` factory block, add:

```python
def make_research_entry_node():
    from google.adk.workflow import node
    return node(research_entry, name="research_entry")
```

Add `"research_entry"` and `"make_research_entry_node"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/runtime/test_research_entry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/parallel_stages.py tests/runtime/test_research_entry.py
git commit -m "feat(pipeline): add research_entry passthrough fan-out source"
```

---

### Task A7: Wire `gap_eval` + edges into `build_pipeline`

**Files:**
- Modify: `aiforge_core/runtime/pipeline.py`
- Test: `tests/runtime/test_pipeline_gap_wiring.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_pipeline_gap_wiring.py
from aiforge_core.runtime import pipeline


def _node_names(wf):
    return {getattr(n, "name", None) for n in wf.graph.nodes}


def test_gap_nodes_present_full_path():
    wf = pipeline.build_pipeline(skip_researcher=False)
    names = _node_names(wf)
    assert {"research_entry", "gap_eval", "gap_gate"} <= names


def test_gap_nodes_absent_when_researcher_skipped():
    wf = pipeline.build_pipeline(skip_researcher=True)
    names = _node_names(wf)
    assert "gap_eval" not in names
    assert "gap_gate" not in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/runtime/test_pipeline_gap_wiring.py -v`
Expected: FAIL — gap nodes absent

- [ ] **Step 3: Wire the nodes + edges in `build_pipeline`**

3a. Import the gap helpers. In the `from .graph_pipeline import (...)`
block add `ROUTE_RESEARCH_GAP`, `ROUTE_RESEARCH_OK`, `make_gap_gate`.
In the `from .parallel_stages import (...)` block add
`make_research_entry_node`.

3b. Add the `gap_eval` import alias near the other agent imports:

```python
from aiforge_core.agents import (
    gap_eval as _gap_eval_mod,
)
```

3c. Build the agent + nodes. After `validator = _validator_mod.build(...)`:

```python
    # Research-gap critic — only meaningful when the Researcher ran.
    gap_eval = _gap_eval_mod.build(build_litellm_model) \
        if not skip_researcher else None
```

3d. `gap_eval` is a tool-less single-shot judge → single_turn (same as
verifiers). In the `for _a in (triage, validator, *verifier_branches):`
loop, change to include gap_eval when present:

```python
    _single_turn = [triage, validator, *verifier_branches]
    if gap_eval is not None:
        _single_turn.append(gap_eval)
    for _a in _single_turn:
        _a.mode = "single_turn"
```

3e. Give gap_eval the same branch retry. In the
`for _b in (triage, enhancer, planner, doer, validator):` retry loop,
append gap_eval when present:

```python
        _crit = [triage, enhancer, planner, doer, validator]
        if gap_eval is not None:
            _crit.append(gap_eval)
        for _b in _crit:
            _b.retry_config = _branch_retry
```

3f. Build the routing nodes. After `validator_gate = make_validator_gate()`:

```python
    research_entry = make_research_entry_node()
    gap_gate = make_gap_gate() if not skip_researcher else None
```

3g. Rewire the context fan-out source + the merge_context out-edge.
Replace the existing context fan-out edges:

```python
    # OLD:
    # for br in context_branches:
    #     edges.append(Edge(from_node=enhancer, to_node=br))
    #     edges.append(Edge(from_node=br, to_node=context_join))
    # edges.append(Edge(from_node=context_join, to_node=merge_context))
    # edges.append(Edge(from_node=merge_context, to_node=planner))
```

with:

```python
    # enhancer → research_entry → (fan-out) → join → merge_context
    edges.append(Edge(from_node=enhancer, to_node=research_entry))
    for br in context_branches:
        edges.append(Edge(from_node=research_entry, to_node=br))
        edges.append(Edge(from_node=br, to_node=context_join))
    edges.append(Edge(from_node=context_join, to_node=merge_context))
    if gap_gate is not None:
        # merge_context → gap_eval → gap_gate ─┬ research_ok → planner
        #                                       └ research_gap → research_entry
        edges.append(Edge(from_node=merge_context, to_node=gap_eval))
        edges.append(Edge(from_node=gap_eval, to_node=gap_gate))
        edges.append(Edge(from_node=gap_gate, to_node=planner,
                          route=ROUTE_RESEARCH_OK))
        edges.append(Edge(from_node=gap_gate, to_node=research_entry,
                          route=ROUTE_RESEARCH_GAP))
    else:
        edges.append(Edge(from_node=merge_context, to_node=planner))
```

3h. Add gap_eval to the chat→single_turn node-collection list only if it
must be in `_agent_nodes` for mode flipping — it is NOT (it is
single_turn, set in 3d). Leave `_agent_nodes` unchanged.

3i. The `max_concurrency` floor of 3 already covers the context fan-out
(≤3 branches). No change needed — but confirm the comment still holds:
the gap re-dispatch re-fires all context branches from `research_entry`
in one wave, exactly the case the floor protects.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/runtime/test_pipeline_gap_wiring.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the full pipeline test suite (no regressions)**

Run: `python -m pytest tests/runtime -v`
Expected: PASS (all prior pipeline/routing tests still green)

- [ ] **Step 6: Commit**

```bash
git add aiforge_core/runtime/pipeline.py tests/runtime/test_pipeline_gap_wiring.py
git commit -m "feat(pipeline): wire gap_eval + research-gap loop into the v6 graph"
```

---

## PART B — LM Studio KV-Cache Quantization

### Task B1: KV-quant in the `lms load` command builder

**Files:**
- Modify: `aiforge_core/runtime/local_starter.py`
- Test: `tests/runtime/test_kv_quant_loadcmd.py` (create)

**Note:** The command-builder is currently inline in the SSH-autostart
function. Step 3 extracts the model-load argument assembly into a small
pure helper `_load_cmd(bin_name, model, ctx, parallel, ttl, kv_bits, kind)`
so it is unit-testable without SSH. The existing call site is updated to
use it.

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_kv_quant_loadcmd.py
from aiforge_core.runtime import local_starter as ls


def test_kv_quant_added_for_text_model():
    cmd = ls._load_cmd("lms", "qwen", ctx=65536, parallel=1, ttl=0,
                       kv_bits=4, kind="text")
    assert "--context-length 65536" in cmd
    assert "--kv-cache-quantization 4" in cmd  # exact flag confirmed in Step 3


def test_kv_quant_skipped_for_vision_model():
    cmd = ls._load_cmd("lms", "nex-vision", ctx=65536, parallel=1, ttl=0,
                       kv_bits=4, kind="vision")
    assert "kv-cache-quantization" not in cmd  # obs-28582: breaks vision


def test_kv_quant_disabled_when_bits_zero():
    cmd = ls._load_cmd("lms", "qwen", ctx=65536, parallel=1, ttl=0,
                       kv_bits=0, kind="text")
    assert "kv-cache-quantization" not in cmd


def test_ttl_appended_when_positive():
    cmd = ls._load_cmd("lms", "qwen", ctx=65536, parallel=1, ttl=900,
                       kv_bits=0, kind="text")
    assert "--ttl 900" in cmd
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/runtime/test_kv_quant_loadcmd.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_load_cmd'`

- [ ] **Step 3: Extract + extend the command builder**

> **Implementer pre-step (one-time, against the live Mac Studio):** confirm
> the exact `lms load` KV-quant flag for the installed LM Studio version
> by running `lms load --help` (the obs-28583 automation is the reference
> for the precise key). The plan assumes `--kv-cache-quantization <bits>`;
> if the installed `lms` uses a different token (e.g. a `--config k=v`
> form), substitute it in the one `KV_FLAG` constant below — nothing else
> changes.

Add near the top of `local_starter.py` (module constants):

```python
# Exact lms KV-quant flag — confirm against `lms load --help` on the
# target host (see Task B1 pre-step). Centralised so a version drift is
# a one-line change.
_KV_FLAG = "--kv-cache-quantization"
```

Add the pure helper (above the SSH-autostart function):

```python
def _load_cmd(bin_name: str, model: str, *, ctx: int, parallel: int,
              ttl: int, kv_bits: int, kind: str) -> str:
    """Assemble the `lms load` command string.

    KV-quant is applied ONLY for text models (kind == 'text') and only
    when kv_bits > 0. MLX vision/embedding models break under KV-cache
    quantization (obs-28582), so they always load full-precision KV.
    """
    cmd = (f"{bin_name} load {model} "
           f"--context-length {ctx} --parallel {parallel}")
    if kv_bits > 0 and kind == "text":
        cmd += f" {_KV_FLAG} {kv_bits}"
    if ttl > 0:
        cmd += f" --ttl {ttl}"
    return cmd
```

In the SSH-autostart function, read the new env + model kind and use the
helper. After the `parallel = max(_int_env("AIFORGE_LMS_PARALLEL", 1), 1)`
line add:

```python
    kv_bits = _int_env("AIFORGE_LMS_KV_BITS", 4)
    kind = _model_kind(model)  # text|vision|embedding — see Step 3b
```

Replace the inline `if model:` load-command block with:

```python
    if model:
        load_cmd = _load_cmd(bin_name, model, ctx=ctx, parallel=parallel,
                             ttl=ttl, kv_bits=kv_bits, kind=kind)
        remote = f"{bin_name} server start && {load_cmd}"
    else:
        remote = f"{bin_name} server start"
```

Update the `log.info(...)` autostart line to include `kv_bits` and
`kind` in its formatted message (append `, kv_bits=%d, kind=%s` and the
two args) so the chosen KV setting is visible in logs.

- [ ] **Step 3b: Add the model-kind classifier**

```python
def _model_kind(model: str | None) -> str:
    """Classify a model id for KV-quant eligibility.

    Vision/embedding MLX models break under KV-cache quantization
    (obs-28582), so they must load full-precision KV. Heuristic on the
    model id + an explicit override map via AIFORGE_LMS_VISION_MODELS
    (comma-separated substrings). Default 'text'."""
    if not model:
        return "text"
    name = model.lower()
    extra = [s.strip().lower() for s
             in os.environ.get("AIFORGE_LMS_VISION_MODELS", "").split(",")
             if s.strip()]
    vision_markers = ["vision", "-vl", "vl-", "llava", "nex-n2-mini"] + extra
    if any(m in name for m in vision_markers):
        return "vision"
    if "embed" in name:
        return "embedding"
    return "text"
```

(`nex-n2-mini` is hard-coded because obs-28582 names it specifically as
the vision model KV-quant broke.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/runtime/test_kv_quant_loadcmd.py -v`
Expected: PASS (4 passed). Also run
`python -m pytest tests/runtime/ -k local_starter -v` for no regressions.

- [ ] **Step 5: Commit**

```bash
git add aiforge_core/runtime/local_starter.py tests/runtime/test_kv_quant_loadcmd.py
git commit -m "feat(serving): 4-bit KV-cache quant in lms load, text-models only"
```

---

### Task B2: Per-model manifest + canonical KV-quant in `load-models.sh`

**Files:**
- Modify: `scripts/load-models.sh`
- Create: `scripts/models.manifest.json`

- [ ] **Step 1: Create the manifest**

```json
// scripts/models.manifest.json
{
  "models": [
    {"id": "qwen3-coder-next", "port": 1234, "kv_bits": 4, "kind": "text"},
    {"id": "Qwen3.6-27B-MLX-4bit", "port": 1235, "kv_bits": 4, "kind": "text"},
    {"id": "nex-n2-mini", "port": 1236, "kv_bits": 0, "kind": "vision"}
  ]
}
```

(Strip the `//` comment line — JSON has no comments; it is shown here for
context only. The vision entry pins `kv_bits: 0` AND `kind: vision` —
belt-and-suspenders against obs-28582.)

- [ ] **Step 2: Drive loads from the manifest in `load-models.sh`**

Add a loop that reads each model and exports the per-model
`AIFORGE_LMS_KV_BITS` before loading, skipping KV-quant for non-text
kinds. Append to `scripts/load-models.sh`:

```bash
# --- KV-quant-aware loads from manifest -------------------------------
MANIFEST="${MANIFEST:-$(dirname "$0")/models.manifest.json}"
if command -v jq >/dev/null 2>&1 && [[ -f "$MANIFEST" ]]; then
  CTX="${AIFORGE_LMS_CTX:-262144}"
  jq -c '.models[]' "$MANIFEST" | while read -r m; do
    id=$(echo "$m"   | jq -r '.id')
    kv=$(echo "$m"   | jq -r '.kv_bits')
    kind=$(echo "$m" | jq -r '.kind')
    flag=""
    if [[ "$kv" -gt 0 && "$kind" == "text" ]]; then
      flag="--kv-cache-quantization $kv"   # match _KV_FLAG in local_starter
    fi
    echo "Loading $id (kind=$kind, kv_bits=$kv)"
    "$LMS" load "$id" --context-length "$CTX" --parallel 1 $flag
  done
fi
```

- [ ] **Step 3: Dry-run the script syntax**

Run: `bash -n scripts/load-models.sh`
Expected: no output (syntax OK).

- [ ] **Step 4: Commit**

```bash
git add scripts/load-models.sh scripts/models.manifest.json
git commit -m "feat(serving): manifest-driven KV-quant loads (reproducible, vision-excluded)"
```

---

### Task B3: Document the env knob + correct stale serving note

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add the env doc + correct the runtime note**

In `README.md`, near the existing `mlx_lm.server` lines (around lines
324-325) and the local-serving description, add:

```markdown
> **Local serving is LM Studio** (`lms`), not raw `mlx_lm.server` — see
> `aiforge_core/runtime/local_starter.py`. Models load via
> `scripts/load-models.sh` driven by `scripts/models.manifest.json`.

**KV-cache quantization** (4× KV memory cut, relieves the ONE-117 OOM):
- `AIFORGE_LMS_KV_BITS` (default `4`) — KV-cache quant bits for **text**
  models. `0` disables. Vision/embedding models always load
  full-precision KV (MLX vision breaks under KV-quant — obs-28582);
  classify extra vision ids via `AIFORGE_LMS_VISION_MODELS` (comma-sep).
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document AIFORGE_LMS_KV_BITS + correct stale serving note"
```

---

### Task B4: Validation probe (manual, documented)

**Files:**
- Create: `evals/kv_quant/README.md`

- [ ] **Step 1: Document the validation procedure**

```markdown
# KV-Quant Validation

Goal: confirm 4-bit KV-quant on text models is quality-neutral + cuts RAM,
and that vision models still load.

## Procedure (per text model, on Mac Studio)
1. Baseline: `AIFORGE_LMS_KV_BITS=0` load; record RAM (Activity Monitor /
   `lms ps`) at the 85K-ctx ceiling probe (reuse obs-28564 long-prompt).
2. Quant: `AIFORGE_LMS_KV_BITS=4` reload; rerun the SAME 85K probe.
3. Compare: output semantically equivalent? RAM lower? Record both.

## Vision regression guard
- Load `nex-n2-mini` via `load-models.sh` (manifest kind=vision) →
  confirm it loads cleanly (no obs-28582 crash) and KV-quant was skipped
  (check the load log shows no `--kv-cache-quantization`).

## Pass criteria
- Text: output equivalent, RAM measurably lower at 85K ctx.
- Vision: loads, KV-quant skipped.
```

- [ ] **Step 2: Commit**

```bash
git add evals/kv_quant/README.md
git commit -m "docs(eval): KV-quant validation + vision regression procedure"
```

---

## Final verification

- [ ] Run the full suite: `python -m pytest tests/ -q` — all green.
- [ ] `bash -n scripts/load-models.sh` — syntax OK.
- [ ] Confirm `build_pipeline(skip_researcher=False)` graph includes
  `research_entry`, `gap_eval`, `gap_gate`; `skip_researcher=True` omits
  `gap_eval`/`gap_gate`.

## Self-review notes (plan author)

- **Spec A coverage:** research_entry (A6), gap_eval node+prompt (A2/A4),
  gap_gate+routes+cap (A1), researcher hint block (A3), registration
  (A5), edge wiring + skip_researcher guard + single_turn + retry (A7),
  parse-failure fail-open (A1 test), trivial fast-path untouched (no edge
  change there). ✓
- **Spec B coverage:** AIFORGE_LMS_KV_BITS gate + text-only + graceful
  flag (B1), vision exclusion via _model_kind + manifest kind (B1/B2),
  manifest reproducibility (B2), env docs + stale-note fix (B3),
  85K-ctx validation + vision guard (B4). ✓
- **Type consistency:** `_load_cmd` signature identical in B1 helper,
  tests, and call site; `_KV_FLAG` is the single source for the flag
  token (B1 + referenced in B2 comment); route constants
  `ROUTE_RESEARCH_GAP`/`ROUTE_RESEARCH_OK` defined A1, used A7;
  `gap_verdict`/`research_gap_brief_md`/`context_brief_md` state keys
  consistent across A1/A2/A3/A4. ✓
- **Open implementer item (flagged, not a placeholder):** exact `lms`
  KV-quant flag token confirmed against the live LM Studio version in
  B1 pre-step; isolated to the `_KV_FLAG` constant.
```
