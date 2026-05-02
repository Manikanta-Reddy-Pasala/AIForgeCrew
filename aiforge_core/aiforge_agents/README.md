# aiforge_agents — pluggable archetype pipeline

9-stage agentic pipeline for ticket-driven code change. ADK-style
archetype registry. AiForgeMemory-grounded. Auto-learning across
tickets. **One local model** for everything — `Qwen3-Coder-Next-MLX-4bit`
served by `mlx_lm` on Mac Studio at 256K context.

```
ticket  ─►  Understander  ─►  Planner  ⇄  Grounder         (REPLAN ≤ 3)
                                ▼
                            Verifier
                                ▼
                            Doer  ⇄  Validator             (CRITIC ≤ 2)
                                ▼
                            Tester
                                ▼
                            Architect  ⇄  Doer             (review-retry ≤ 1)
                                ▼                       └► gh pr create
                            Learner  ─►  skills + failures  (Postgres)
```

Each stage is a `BaseArchetype` subclass registered via `@register("name")`
in `archetypes/`. Configuration is 4-layer:

> ctx kwargs > per-repo `.aiforge/agents.yaml` > `~/.aiforge/agents.yaml` > bundled `agents.defaults.yaml`

## Provider per archetype

Every archetype can run on a different provider. Edit live via the UI
**Settings** page (`/ui/settings`) or via API:

```bash
curl http://localhost:8799/api/config/agents              # GET map
curl -X PUT http://localhost:8799/api/config/agents/doer \
  -H 'Content-Type: application/json' \
  -d '{"provider":"ollama_cloud","model":"qwen3-coder-next"}'
```

Available providers: `local` (default — single Mac Studio model),
`anthropic`, `ollama_cloud`. Add API keys to runtime env to enable
the cloud ones. The 9 archetype keys are
`understander | planner | verifier | grounder | doer | validator |
tester | architect | learner`.

## Robust against local-model quirks

`runtime/llm_client.call_json` has a 5-stage tolerant JSON parser plus
a single auto-retry with stricter prompt:

1. strip 3+ backtick fences
2. strict `json.loads`
3. balanced `{...}` regex extract
4. first `{` to last `}` slice
5. retry once with stricter system prompt + temperature 0

Local models (mlx-lm Qwen3-Coder, Ollama Cloud llama variants) routinely
emit 4+ trailing backticks, prose around the JSON object, or markdown
wrappers despite `response_format=json_object`. The parser handles all
three; the retry handles the rest.

## Per-stage docs

| Stage | What it does | How it auto-learns |
|---|---|---|
| [Understander](docs/understander.md) | parses ticket + auto-fetches URLs + queries AiForgeMemory | reads enriched memory; web pages summarised inline |
| [Planner](docs/planner.md) | many small single-file plan steps | `skills_hint` + `failures_hint` in prompt; REPLAN on Grounder fail |
| [Grounder](docs/grounder.md) | rule-based path validation, order-aware | rejects feed Planner REPLAN + become Learner failures |
| [Verifier](docs/verifier.md) | critic on plan-vs-Understanding | issues become Learner episodic rows |
| [Doer](docs/doer.md) | one LLM call per file, git apply + commit | `failures_hint` in prompt; per-step CRITIC retry |
| [Validator](docs/validator.md) | heuristic gate on Doer detector hits | hits roll up to `aiforge_agents_failures` |
| [Tester](docs/tester.md) | TDD test specs | `failures_hint` biases tests toward bug-prone areas |
| [Architect](docs/architect.md) | review + draft MR + `gh pr create` | `failures_hint` flags repeats; comments trigger Doer retry |
| [Learner](docs/learner.md) | distil tickets to skills + failures | the **engine** of auto-correction |

## Detectors (failure-taxonomy ground truth)

| Mode | Detector | Catches |
|---|---|---|
| F-001 | `HallucinatedImportDetector` | imports outside stdlib + graph + plan_create_fqns; filters Java `static`/`type` syntax tokens |
| F-002 | `HallucinatedSymbolDetector` | symbols not in `Symbol_v2` and not under stdlib prefix |
| F-003 | `DiffContextHashDetector` | unified-diff context lines that don't hash-match target file |
| F-004/7/8/10 | `LoopDetector` | same output 3× consecutive |
| F-006 | `check_plan_depth` | plan steps > 12 |
| F-009 | `check_token_budget` | actual tokens > 2× expected |

**Stdlib allowlist** (F-001 pass without graph lookup):

```
java.* javax.* jakarta.* kotlin.* kotlinx.*
org.springframework.* reactor.* io.reactivex.*
org.slf4j.* ch.qos.logback.* org.apache.logging.*
com.fasterxml.jackson.* com.google.gson.*
lombok.* io.swagger.* springfox.*
org.junit.* org.mockito.* org.assertj.* org.hamcrest.*
com.mongodb.* io.nats.* io.lettuce.* redis.clients.*
com.google.common.* com.google.protobuf.*
org.apache.* org.eclipse.*
```

## Auto-learning loop (cross-ticket auto-correction)

```
   ┌──────────────┐
   │   Ticket N   │
   └──────┬───────┘
          ▼
 understander → planner → … → architect → learner ─┐
                                                   │
                            ┌──────────────────────┘
                            ▼
                  Postgres: skills + failures
                            │
                            │  top_skills_for(repo, task_class)
                            │  top_failures_for(repo, task_class)
                            ▼
   ┌──────────────┐
   │  Ticket N+1  │  ← Planner / Doer / Tester / Architect prompts
   └──────────────┘    embed both lists via render_failures_block()
```

Failure-recall headers are contextually framed:

| Stage | Header |
|---|---|
| Planner | `# Mistakes from prior similar tickets — DO NOT REPEAT` |
| Doer | `# Mistakes from prior similar tickets — DO NOT REPEAT` |
| Tester | `# Mistakes from prior tickets — write tests covering these` |
| Architect | `# Mistakes from prior reviews — flag if seen here` |

## Allowed-files seeding (prevents Planner hallucination)

Orchestrator pulls top-K relevant File_v2 paths from AiForgeMemory once
per ticket and injects them into the Planner prompt as the strict
allowlist. Sources combined:

1. bge-m3 vector top-K via Cypher `codemem_chunk_embed`
2. CamelCase-aware Lucene fulltext on `Symbol_v2.signature`
3. Service `CONTAINS_FILE` neighbours
4. Cross-encoder rerank on the top-30
5. 1-hop graph expansion (IMPORTS in/out)

`.aiforge-worktrees/**` and other dotfile paths are filtered out.

## REPLAN loop (Planner ⇄ Grounder)

```
plan_attempts = 0
while plan_attempts < 3:
    plan = planner.run(ctx={...})
    plan, dropped = filter_plan_targets(plan, allowed_files)  # post-filter
    grounding = grounder.run(ctx={"plan": plan, "repo": repo})
    if kept_steps == 0:                # empty-plan REPLAN
        force unresolved + retry
    if grounding.resolved and shrink_ratio < 0.5:
        break
    plan_attempts += 1
```

Grounder leniencies:

- `action=create` rejected if file already exists in graph (suggest
  `edit`); else passes if any ancestor dir has at least one indexed
  file.
- `action=read|edit|test` passes if the path appears as a `create`
  target earlier in the same plan (order-aware).

## CRITIC loop (Doer ⇄ Validator)

```
for step in write_steps:
    for attempt in range(2):
        outcome = doer.run(ctx={..., target_step=step,
                                previous_udiff=...,
                                detector_problems=...})
        v = validator.run(outcome)
        if v.approve or v.skip: break
        previous_udiff, previous_problems = outcome.udiff, outcome.problems
```

Architect→Doer retry: when validator approves but Architect requests
changes, one more Doer pass receives the comments as feedback.

## Multi-step Doer + git apply

Each ticket gets one branch `aiforge/<ticket>`. Per step:

- First step: branch from remote default (`master`/`main`) detected
  via `git symbolic-ref refs/remotes/origin/HEAD`.
- Subsequent steps: checkout the existing branch (commits stack).
- `git apply --recount --ignore-whitespace --whitespace=nowarn`.
- Commit with `:(exclude)` for `.aiforge`, `.aiforge-worktrees`,
  `graphify-out`, `.idea`, `.vscode`.

## Configuration

```yaml
# .aiforge/agents.yaml  (per-repo override; ~/.aiforge/agents.yaml is global)
archetypes:
  understander:
    model: /path/to/Qwen3-Coder-Next-MLX-4bit
    temperature: 0.3
    max_tokens: 8000
  planner:
    temperature: 0.3
    max_tokens: 24000
  doer:
    max_tokens: 24000
```

Bundled `agents.defaults.yaml` ships sensible defaults so a fresh repo
runs without any operator config.

## CLI

```bash
# Understander → Planner → … → Learner, no apply, no PR
python -m aiforge_core.aiforge_agents.orchestrator.run_ticket \
    --repo PosClientBackend \
    --title "Add ledgerCategory CRUD APIs" \
    --body  "Controller + Service + Impl + Repository ..."

# Apply Doer diff to a ticket branch + open the GitHub PR
AIFORGE_AGENTS_APPLY=1 \
AIFORGE_AGENTS_OPEN_MR=1 \
python -m aiforge_core.aiforge_agents.orchestrator.run_ticket \
    --repo PosClientBackend --title "..." --body "..."
```

The orchestrator also mirrors every stage's outcome into the existing
`tickets` table (status + metadata) so the AIForgeCrew web UI surfaces
the run automatically (Body, Plan steps, Doer udiff, Architect MR with
the GitHub PR link).

## Tests

```bash
.venv/bin/python -m pytest aiforge_core/aiforge_agents/tests/ -q
```

73 unit tests across registry, runtime helpers, detectors, archetypes,
prompt helpers, and web-fetch URL extraction.

| File | Covers |
|---|---|
| `test_archetypes_unit.py` | Doer / Validator / Tester / Architect / Learner / filter / git apply / skills / compaction / failures-block / web URL extract |
| `test_detectors.py` | F-001 stdlib, sibling-create, wildcard, static-import filter, F-002, F-003, depth, budget |
| `test_circuit_breakers.py` | wall-clock, retries, token budget, audit |
| `test_failure_taxonomy.py` | mode registration + recovery actions |
| `test_registry.py` | 4-layer config lookup |
| `test_runtime.py` | LLM client mocks |

## Recent changes (May 2026)

- Multi-step Doer (one LLM call per file) — branch stacks commits.
- Auto-learn end-to-end: `aiforge_agents_failures` + `_skills`,
  surfaced into Planner/Doer/Tester/Architect prompts.
- Context compaction in every prompt via shared `runtime/prompt_helpers`.
- Granular plans cap 7 → 12; post-Planner allowlist filter; empty-plan
  REPLAN guard.
- URL auto-learn (`runtime/web_fetch.py`) — Understander auto-summarises
  any URL in title/body.
- `git apply` with `--recount --ignore-whitespace`; auto base-branch
  detection; `gh pr create --draft` on `request_changes`.
- mlx_lm server bumped to `--max-tokens 32768`; model supports 256K
  context natively (`max_position_embeddings: 262144`).
- F-001 false-positives fixed: extended stdlib (Spring/Lombok/Swagger/
  JUnit/Mockito/Mongo/NATS), Java `import static` syntax, sibling-FQN
  awareness.
