# aiforge_agents — pluggable archetype pipeline

9-stage agentic pipeline for ticket-driven code change. ADK-style
archetype registry, single-LLM friendly, AiForgeMemory-grounded.

```
ticket  ─►  Understander  ─►  Planner  ⇄  Grounder  (REPLAN ≤ 3)
                                ▼
                           Verifier
                                ▼
                            Doer  ──►  Validator
                                ▼
                            Tester
                                ▼
                            Architect  ─►  Learner
```

Each stage is a `BaseArchetype` subclass registered via `@register("name")`
in `archetypes/`. Configuration is 4-layer: ctx kwargs > per-repo
`.aiforge/agents.yaml` > `~/.aiforge/agents.yaml` > bundled
`agents.defaults.yaml`.

---

## Stages

| Stage | What it produces | Failure mode |
|---|---|---|
| **Understander** | structured `understanding` (problem, knowns, unknowns, risks) + `context_md` from AiForgeMemory | `llm_invalid_json` → empty fields |
| **Planner** | `plan.steps[]` (read/edit/test/run/create), allowlisted to AiForgeMemory file paths | depth>7 → `F-006` |
| **Grounder** | resolves every step target against `File_v2` (graph). Order-aware: later steps can reference files earlier steps create | `unresolved_refs[]` triggers REPLAN ≤ 3× |
| **Verifier** | `verdict ∈ {pass, repair, reject}` + `issues[]` post-grounding | invalid JSON → `repair` (safe default) |
| **Doer** | unified diff for first edit/create step → `.aiforge/proposals/<ticket>.patch` | F-001 / F-003 detector blocks |
| **Validator** | approve / block / skip on Doer outcome (looks at detector problems) | block on F-001 hallucinated imports |
| **Tester** | TDD test specs (junit5 / pytest) | `llm_invalid_json` → `tests:[]` |
| **Architect** | review decision + draft MR title/body | `request_changes` if validation blocked |
| **Learner** | episodic + procedural rows in Postgres | always best-effort |

---

## Detectors (failure-taxonomy ground truth)

| Mode | Detector | What it catches |
|---|---|---|
| F-001 | `HallucinatedImportDetector` | imports not in stdlib allowlist + not in graph + not a sibling being created in same plan |
| F-002 | `HallucinatedSymbolDetector` | symbols not in `Symbol_v2` and not under stdlib prefix |
| F-003 | `DiffContextHashDetector` | unified-diff context lines that don't hash-match target file |
| F-004/7/8/10 | `LoopDetector` | same output 3× consecutive (per ticket × step kind) |
| F-006 | `check_plan_depth` | plan.steps > 7 |
| F-009 | `check_token_budget` | actual_tokens > 2× expected |

**Stdlib allowlist** (F-001 pass without graph lookup):

```
java.* javax.* jakarta.* kotlin.* kotlinx.*
org.springframework.* reactor.* io.reactivex.*
org.slf4j.* ch.qos.logback.* org.apache.logging.*
com.fasterxml.jackson.* com.google.gson.*
lombok.* io.swagger.* springfox.*
org.apache.* org.eclipse.*
org.junit.* org.mockito.* org.assertj.* org.hamcrest.*
com.mongodb.* io.nats.* io.lettuce.* redis.clients.*
com.google.common.* com.google.protobuf.*
```

Sibling-create FQNs (Java/Kotlin paths under `src/{main,test}/{java,kotlin}/`)
are computed from the plan's `action=create` steps and treated as known —
prevents false positives when one plan step imports a class another step
is creating.

---

## Allowed-files seeding (prevents Planner hallucination)

Orchestrator pulls top-K relevant File_v2 paths from AiForgeMemory once
per ticket and injects them into the Planner prompt as the strict
allowlist. Sources combined:

1. bge-m3 vector top-K via Cypher `codemem_chunk_embed`
2. CamelCase-aware Lucene fulltext on `Symbol_v2.signature`
3. Service `CONTAINS_FILE` neighbours

`.aiforge-worktrees/**` and other dotfile paths are filtered out — prior
agent worktree dirs were polluting the index and crowding out real
source files (5040 → 1007 canonical files on PCB after purge).

The set is also stored in ticket metadata as `allowed_files` (head 40 +
count) for post-mortem visibility.

---

## REPLAN loop (Planner ⇄ Grounder)

```
plan_attempts = 0
while plan_attempts < 3:
    plan = planner.run(ctx={
        "understanding": ...,
        "allowed_files": allowed_files,
        "previous_plan": last_plan,
        "unresolved_refs": grounder.unresolved_refs,
    })
    grounding = grounder.run(ctx={"plan": plan, "repo": repo})
    if grounding.resolved: break
    plan_attempts += 1
```

The Planner prompt receives an explicit "BLOCKED — replace these refs"
block listing the previous attempt's unresolved targets.

Grounder leniencies:

- `action=create` passes if **parent OR grandparent** dir exists
  (allows fresh feature packages: `feature/storeregion/` under
  existing `feature/`).
- `action=read|edit|test` passes if the path appears as a `create`
  target earlier in the same plan (order-aware).

---

## Configuration

```yaml
# .aiforge/agents.yaml  (per-repo override; ~/.aiforge/agents.yaml is global)
archetypes:
  understander:
    model: /path/to/Qwen3-Coder-Next-MLX-4bit
    temperature: 0.3
    max_tokens: 2048
  planner:
    temperature: 0.2
    max_tokens: 6000
    grammar: plan.gbnf
```

Bundled `agents.defaults.yaml` ships sensible defaults so a fresh repo
runs without any operator config.

---

## CLI

```bash
python -m aiforge_core.aiforge_agents.orchestrator.run_ticket \
    --repo PosClientBackend \
    --title "Add ledgerCategory CRUD APIs" \
    --body  "Controller + Service + Impl + Repository ..."
```

Prints JSON with `understanding`, `plan`, `grounding`, `doer_outcome`,
`validation`, `test_plan`, `review`, `learning`, `latency_s`.

Postgres mirroring: orchestrator writes the same data into `tickets`
(status + metadata) so the AIForgeCrew web UI shows the run.

---

## Tests

```bash
.venv/bin/python -m pytest aiforge_core/aiforge_agents/tests/ -q
```

56/56 unit tests across registry, runtime, detectors, archetypes.

| File | Covers |
|---|---|
| `test_archetypes_unit.py` | Doer / Validator / Tester / Architect / Learner |
| `test_detectors.py` | F-001 stdlib allowlist, sibling-create, wildcard imports, F-002, F-003 |
| `test_circuit_breakers.py` | wall-clock, retries, token budget, audit |
| `test_failure_taxonomy.py` | mode registration + recovery actions |
| `test_registry.py` | 4-layer config lookup |
| `test_runtime.py` | LLM client mocks, memory mocks |

---

## Recent changes (May 2026)

- **F-001 stdlib allowlist** extended (Spring/Lombok/Swagger/Jackson/
  JUnit/Mockito/Mongo/NATS) — was flagging every standard import.
- **Plan-create FQN sibling awareness** — Doer-emitted imports of
  files another plan step creates no longer false-positive.
- **Order-aware Grounder** + grandparent-dir leniency — fresh
  feature packages (e.g. `feature/storeregion/`) now resolve.
- **Worktree filter in `_fetch_allowed_files`** — `.aiforge-worktrees/**`
  paths dropped before allowlist seeding.
- **Doer `action=create`** — new file generation supported alongside
  `edit`; emits `--- /dev/null` udiff.
- **Learner shallow-path fix** — `task_class` derivation no longer
  crashes on top-level targets like `README.md`.
- **AiForgeMemory translator** got hybrid retrieval (RRF + cross-encoder
  rerank + CamelCase + synonym expansion + 1-hop graph) — see
  `AiForgeMemory/README.md`.
