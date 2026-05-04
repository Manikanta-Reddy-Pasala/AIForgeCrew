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

## REPLAN loop (Planner ⇄ Grounder ⇄ Verifier)

```
plan_attempts = 0
while plan_attempts < 3:
    plan = planner.run(ctx={..., unresolved_refs=[*grounder, *verifier_issues]})
    plan, dropped = filter_plan_targets(plan, allowed_files)  # post-filter
    grounding = grounder.run(ctx={"plan": plan, "repo": repo})
    if kept_steps == 0:                # empty-plan REPLAN
        force unresolved + retry
    if grounding.resolved and shrink_ratio < 0.5:
        verdict = verifier.run(ctx={...})       # critic on grounded plan
        if verdict == "reject": carry issues, replan
        else: break
    plan_attempts += 1
```

Verifier sits inside the loop. `verdict=reject` carries issues forward
as synthetic unresolved refs and re-invokes Planner. Cap 3 attempts.

Grounder leniencies:

- `action=create` rejected if file already exists in graph (suggest
  `edit`); else passes if any ancestor dir has at least one indexed
  file.
- `action=read|edit|test` passes if the path appears as a `create`
  target earlier in the same plan (order-aware).

## CRITIC loop (Doer ⇄ Validator)

```
for step in write_steps:
    head_before_step = git rev-parse HEAD            # for rollback
    for attempt in range(AIFORGE_CRITIC_MAX):  # default 3
        outcome = doer.run(ctx={..., target_step=step,
                                previous_udiff=...,
                                detector_problems=...})
        v = validator.run(outcome)
        if v.approve or v.skip: break
        if udiff identical OR problem count grew:    # diminishing-returns
            rollback to head_before_step; bail
        previous_udiff, previous_problems = outcome.udiff, outcome.problems
    else:
        rollback to head_before_step                 # full exhaust
```

Cap env-tunable via `AIFORGE_CRITIC_MAX` (default 3). Diminishing-returns
guard bails early when the next attempt's udiff is byte-identical or
problem set grew. On terminal failure (CRITIC exhausted OR bail) the
step's commit is `git reset --hard`-ed to `head_before_step` so the
final PR carries only validator-approved commits.

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

## Resilience knobs (env-tunable)

| Var | Default | Purpose |
|---|---|---|
| `AIFORGE_CRITIC_MAX` | `3` | Per-step Doer attempts (Doer + retries) |
| `AIFORGE_LLM_RETRY_MAX` | `3` | Per-endpoint LLM retries on 5xx/429/conn |
| `AIFORGE_LLM_RETRY_BASE_S` | `0.5` | LLM backoff base seconds |
| `AIFORGE_LLM_RETRY_CAP_S` | `8.0` | LLM backoff cap seconds |
| `AIFORGE_GH_RETRY_MAX` | `3` | `git push` / `gh pr create` retries |
| `AIFORGE_GH_RETRY_BASE_S` | `1.0` | gh backoff base |
| `AIFORGE_GH_RETRY_CAP_S` | `10.0` | gh backoff cap |
| `AIFORGE_LEARNER_HALFLIFE_DAYS` | `30` | Skill/failure decay half-life |
| `AIFORGE_LEARNER_CUTOFF_DAYS` | `180` | Drop entries older than this |
| `AIFORGE_RUN_TESTS` | `0` | Set `1` to actually run repo tests |
| `AIFORGE_RUN_TESTS_TIMEOUT_S` | `600` | Test run timeout (Maven can be slow) |
| `AIFORGE_RUNS_DIR` | `~/.aiforge/runs` | Trace + spec artifacts root |

## Time-decayed skill / failure ranking

Old failures should not haunt new tickets forever. `top_failures_for`
and `top_skills_for` rank by:

```
score = seen_count * power(0.5, age_days / half_life_days)
```

Half-life 30d means a 30-day-old failure weighs half a fresh one,
60-day-old weighs a quarter, etc. Entries older than `cutoff_days`
are dropped entirely. SQL pushes the math down to Postgres; pure-python
mirror in `_decay_factor` for in-memory ranking.

## Tester actually runs tests (opt-in)

After spec persistence, when `AIFORGE_RUN_TESTS=1` and the diff was
applied + validator-approved, the orchestrator detects the framework
and runs it:

| Marker file | Framework | Command |
|---|---|---|
| `pom.xml` | maven | `mvn test -q -DskipITs=true` (or `./mvnw`) |
| `build.gradle{,.kts}` | gradle | `gradle test --quiet` (or `./gradlew`) |
| `package.json` | npm | `npm test --silent` |
| `pyproject.toml` / `requirements.txt` | pytest | `pytest -q` |

Tail-parses pass/fail counts, records `tester.tests_executed` audit,
attaches results to `test_plan.execution`. Failure does not abort the
pipeline — Architect already gates merge. HITL escalation fires on red.

## HITL escalation

When terminal failures stack up, the orchestrator sets ticket
`status=needs_human`, writes `~/.aiforge/runs/<ticket>/hitl_request.md`,
and returns a `hitl` block in the response. Trigger conditions (any):

- circuit breaker tripped (LLM provider down)
- Verifier still rejects after 3 REPLAN attempts
- Grounder never resolved
- Validator blocked AND >= 3 Doer attempts used
- Architect rejected the diff
- Tests actually executed AND failed

The HITL markdown lists evidence + plan summary + Architect comments —
ready for a human to pick up.

## Tester specs persisted

Each run writes `~/.aiforge/runs/<ticket>/test_plan.json` and registers
it as an attachment with `role=tester_specs`. Survives process death,
shows up in the UI's attachment list, becomes the input for #2's
`AIFORGE_RUN_TESTS=1` execution stage.

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
python -m aiforge_core.orchestrator.run_ticket \
    --repo PosClientBackend \
    --title "Add ledgerCategory CRUD APIs" \
    --body  "Controller + Service + Impl + Repository ..."

# Apply Doer diff to a ticket branch + open the GitHub PR
AIFORGE_AGENTS_APPLY=1 \
AIFORGE_AGENTS_OPEN_MR=1 \
python -m aiforge_core.orchestrator.run_ticket \
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

201 unit tests across registry, runtime helpers, detectors, archetypes,
prompt helpers, web-fetch URL extraction, transport retry, gh retry,
verifier REPLAN, Doer rollback, CRITIC cap, learner decay, Tester
specs persistence, Tester run framework + parsing, HITL classifier.

| File | Covers |
|---|---|
| `test_archetypes_unit.py` | Doer / Validator / Tester / Architect / Learner / filter / git apply / skills / compaction / failures-block / web URL extract |
| `test_detectors.py` | F-001 stdlib, sibling-create, wildcard, static-import filter, F-002, F-003, depth, budget |
| `test_circuit_breakers.py` | wall-clock, retries, token budget, audit |
| `test_failure_taxonomy.py` | mode registration + recovery actions |
| `test_registry.py` | 4-layer config lookup |
| `test_runtime.py` | LLM client mocks |
| `test_llm_router_health.py` | Provider routing + health gating |
| `test_llm_transport_retry.py` | 5xx/429/conn retry + Retry-After |
| `test_architect_gh_retry.py` | gh push/PR retry + already-exists fallback |
| `test_verifier_replan.py` | Verifier-reject → REPLAN; cap at 3 |
| `test_doer_rollback.py` | git head/reset helpers + step rollback |
| `test_critic_retry_cap.py` | CRITIC cap=3 + diminishing-returns guards |
| `test_learner_decay.py` | half-life math; recent beats stale |
| `test_tester_specs_persist.py` | test_plan.json write + attachment |
| `test_tester_runs.py` | framework detect (mvn/gradle/npm/pytest) + parse |
| `test_hitl_escalation.py` | classifier triggers + persistence |
| `test_recovery_engine.py` | F-006 plan-depth + repeat-replan escalation |
| `test_workflows.py` | trial-balance flow harness |

## Recent changes (May 2026)

- Per-archetype provider routing wired end-to-end — Settings UI per-role
  choice (provider + model) flows through the router.
- LLM transport retry — per-endpoint exp backoff on 5xx/429/conn-reset
  with Retry-After honored. Knobs: `AIFORGE_LLM_RETRY_*`.
- Architect `git push` + `gh pr create` retry; "PR already exists" falls
  back to `gh pr view` URL instead of returning empty.
- Verifier moved INSIDE the REPLAN loop — `verdict=reject` carries issues
  forward as synthetic unresolved refs and re-invokes the Planner.
- Doer per-step rollback — failed steps `git reset --hard` back to
  pre-step HEAD so PR carries only validator-approved commits.
- CRITIC retry cap bumped 2 → 3 with diminishing-returns guard
  (identical udiff or growing problem set bails early).
- Time-decayed skill/failure ranking — `score = seen × 0.5^(age/half_life)`,
  default half-life 30d, cutoff 180d. SQL push-down + python mirror.
- Tester specs persisted to `~/.aiforge/runs/<ticket>/test_plan.json`
  and registered as an attachment.
- Tester actually runs `mvn test` / `gradle test` / `npm test` / `pytest`
  when `AIFORGE_RUN_TESTS=1`. Framework auto-detect + tail-parse.
- HITL escalation — terminal-failure stack triggers `status=needs_human`
  + writes `hitl_request.md` for a human to pick up.
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
