aiforge_agents
Technical Specification — v0.4
Local-models-only autonomous AI dev team
ADK-based runtime · 8 harnesses · single repo · in-house everything
Manikanta Reddy Pasala  |  v0.4  |  May 2026

Changelog v0.3 → v0.4
v0.4 is a full rewrite around three decisions:
	•	Paperclip is replaced — Orchestrator is now in-house
	•	Hermes is replaced — Runtime is in-house, built on Google ADK primitives
	•	Single repo: aiforge_agents
	•	Eight harnesses, each with explicit scope: Orchestrator, Runtime, Inference, Memory, Eval, Sandbox, Observability, HITL
	•	Budgets removed from Orchestrator (deferred); circuit breakers stay in Runtime as safety, not budget
	•	No public ticket API in v1 — humans create tickets through the HITL Web UI
	•	All v0.3 hardening preserved: Verifier, Grounder, CRITIC loop, hallucinated-import killer, Stuck Detector, prompt registry, three-tier compaction, skill tree
1. Goal
Build aiforge_agents — a local-models-only autonomous AI dev team that handles human-created tickets end-to-end and produces a reviewable Merge Request. No external orchestrator, no external agent runtime, no cloud LLM at runtime.
1.1 The work flow
Human (HITL Web UI) → Ticket
   ↓
Orchestrator assigns roles, opens session
   ↓
Understander → Planner → Verifier → Grounder
                                        ↓
                          [Tester ↔ Doer]  (CRITIC loop)
                                        ↓
                                   Architect → MR
                                        ↓
                                  Human merges
1.2 Non-goals (v1)
	•	Cloud LLM at runtime
	•	External public ticket API
	•	Per-team budgets / cost ceilings (deferred to v2)
	•	SolAgents adapter (separate doc)
	•	Multi-tenant memory routing
	•	Custom agent runtime — we use Google ADK
2. External dependencies
Three external components. All other functionality is in-house in this repo.
Dependency
Role
Where
Google ADK
Agent runtime primitives — LlmAgent, LoopAgent, Runner, SessionService, callbacks
Python package
AiForgeMemory
Code intelligence — Neo4j-backed graph, NL → ContextBundle
github.com/Manikanta-Reddy-Pasala/AiForgeMemory
Local inference servers
llama.cpp / MLX-LM on Mac Studio M4 Max 128GB
Local
Postgres, Neo4j, Docker, and Next.js are infrastructure but not external in the same sense — they are dependencies our containers carry.
3. Architecture overview
3.1 Eight harnesses
#
Harness
Responsibility
1
Orchestrator
Replaces Paperclip — tickets, org chart, governance, MR gate
2
Runtime
Replaces Hermes — ADK-based agent loop, callbacks, tool registry, circuit breakers, kill-switches, failure-taxonomy detectors
3
Inference
Local model pool, prefix cache, grammar-constrained decoding, sampling, hot-swap
4
Memory
Unified Memory Client over AiForgeMemory + Postgres + skill/session/prompt files
5
Eval
20 fixed eval tickets + replay engine + golden traces + prompt A/B + unit/integration/e2e/load tests
6
Sandbox
Docker pool, allowlists, warm-container reuse
7
Observability
Auditor middleware, traces, metrics, cost, prefix-cache hit rate, append-only audit, redaction
8
HITL
Next.js Web UI: ticket creation, approval queue, replay viewer, skill/prompt browsers
3.2 Diagram
┌──────────────────────────────────────────────────────────────────┐
│                  HITL WEB UI  (harness 8)                        │
│  Ticket creation · Approval queue · Replay · Skill/prompt browse │
└─────────┬────────────────────────────────────────────────────────┘
          ▼  (writes directly, no public API)
┌──────────────────────────────────────────────────────────────────┐
│                  ORCHESTRATOR  (harness 1)                       │
│  Tickets · Org chart · Governance · MR gate                      │
└─────────┬────────────────────────────────────────────────────────┘
          ▼
┌──────────────────────────────────────────────────────────────────┐
│                  RUNTIME  (harness 2 — built on ADK)             │
│                                                                   │
│  ADK Runner ──► [Understander → Planner → Verifier → Grounder]   │
│                                          ↓                        │
│                              [Tester ↔ Doer]  (CRITIC)            │
│                                          ↓                        │
│                                     Architect → MR                │
│                                                                   │
│  ADK callbacks: Compactor · Auditor · Circuit Breakers ·         │
│                 Stuck Detector · Failure-Taxonomy · Learner-hook  │
└──┬──────────┬─────────────────┬──────────────────┬──────────────┘
   ▼          ▼                 ▼                  ▼
┌─────┐  ┌─────────┐     ┌──────────┐    ┌──────────────┐
│ INF │  │ MEMORY  │     │ SANDBOX  │    │ OBSERVABILITY│
│  3  │  │   4     │     │    6     │    │      7       │
└─────┘  └─────────┘     └──────────┘    └──────────────┘
                                                 │
                                                 ▼
                                  Postgres (audit/episodic/procedural)
                                  + Neo4j (AiForgeMemory)
                                  + Files (.aiforge/skills, prompts, sessions)
4. Orchestrator harness (#1)
Replaces Paperclip. In-house Python service that owns ticket lifecycle, role assignments, governance, and the merge-request gate.
4.1 Components
Component
Responsibility
tickets.py
Ticket lifecycle: created → planning → executing → reviewing → mr_open → merged | failed | abandoned
org_chart.py
Per-ticket role assignment: which Understander/Planner/Doer/etc. instance handles this ticket
governance.py
Rules: agents cannot merge; MR must pass eval gate; every ticket must have HITL approval before merge
mr_gate.py
Pre-merge checks: tests pass, lint pass, no failure-taxonomy hits unresolved, human approval present
4.2 Ticket model
{
  "id": "TKT-2026-0001",
  "title": "Add pagination to /users endpoint",
  "body": "...markdown body with acceptance criteria...",
  "repo": "PosClientBackend",
  "branch_base": "main",
  "labels": ["api", "backend"],
  "created_by": "human:manikanta",
  "created_at": "...",
  "state": "executing",
  "assigned": {
    "understander": "agent-instance-uuid",
    "planner": "...", "verifier": "...", "grounder": "...",
    "doer": "...", "tester": "...", "architect": "..."
  },
  "artifacts": {
    "understanding_id": "...",
    "plan_id": "...",
    "verifier_verdict_id": "...",
    "mr_url": null
  },
  "circuit_breaker_state": "ok",
  "wall_clock_started_at": "..."
}
4.3 State machine
created
  └─► understanding ─► planning ─► verifying ─► grounding ─► executing
                                                                  │
                                                                  ▼
                                                            reviewing
                                                                  │
                                                                  ▼
                                                          mr_open ──► merged
                                                            │
                                                            └──► abandoned (human reject)
                                                            └──► failed   (circuit breaker)
4.4 Governance rules (hard-coded, not configurable)
	•	Agents never merge — only human approval through HITL Web UI promotes mr_open → merged
	•	MR can only open after Architect's review and Tester's tests-green
	•	Every state transition writes an audit event
	•	A ticket in failed state can be re-opened only by a human
5. Runtime harness (#2)
Replaces Hermes. In-house code that wraps Google ADK primitives. Where ADK ends and our code begins:
5.1 ADK vs in-house split
ADK provides
We build on top
LlmAgent, LoopAgent, SequentialAgent, ParallelAgent
Archetype subclasses (Understander, Planner, Doer, etc.)
Runner, SessionService, state management
agent_runner.py — wires ADK Runner to Orchestrator + Memory
before_model / after_model / before_tool / after_tool callbacks
Compactor, Auditor, Circuit Breakers, Stuck Detector, Learner hook — all as ADK callbacks
Tool framework (function declarations + dispatch)
tool_registry.py with our atomic tools (shell, fs, git, http, KGR, code-review-graph)
Streaming output via run_async
Output schema enforcement (grammar-constrained) wraps the stream
5.2 ADK callbacks doing real work
This is the heart of the runtime. Eight callbacks chained in a fixed order around every model/tool call.
Order
Callback
When
Function
1
auditor.before
before_model + before_tool
Pre-record intent into audit_events
2
circuit_breakers.check
before_model + before_tool
Trip if budget/wall-clock/retry limits hit
3
compactor.maybe_microcompact
before_model
Drop tool_result blocks >5 steps old
4
compactor.maybe_full_compact
before_model
Trigger summarisation at 60% window
5
stuck_detector.check
after_model
No-progress heuristic
6
failure_taxonomy.match
after_model + after_tool
Match output against F-001..F-012
7
auditor.after
after_model + after_tool
Record outcome + duration + status
8
learner_hook.notify
after_model + after_tool
Async write step_trace + update procedural
5.3 Tool registry
Atomic primitives only. Higher-level capabilities are built as skills (see harness 4).
Tool
Purpose
Sandbox required
shell.run
Execute shell command (lint, test, build)
Yes — Docker
fs.read / fs.write / fs.delete
File ops within working tree
Yes — Docker volume
git.diff / git.commit / git.apply_udiff / git.log
VCS within working tree
Yes
http.get
External knowledge fetch (KGR only)
No, but redacted
code_review_graph.query
MCP query against AiForgeMemory
No
memory.expand
Dereference a memory_id to full content
No
sandbox.exec_python
smolagents-style code-as-action
Yes — sandboxed Python
knowledge_gap_resolver.resolve
Web/SO/self-distill
No
5.4 Failure-taxonomy detectors
Twelve known failure modes. Detectors run as part of the failure_taxonomy.match callback.
ID
Failure mode
Detector
F-001
Hallucinated import
Import not in IMPORTS graph + not in pkg manifest
F-002
Hallucinated symbol
Symbol not in Symbol_v2 nodes for repo
F-003
Diff context mismatch
Hash of context lines != target file
F-004
Test loop without progress
Same failing-test set 3 iterations
F-005
Plan with unreachable step
Grounder fails to resolve a step's references
F-006
Plan exceeds depth limit
steps.length > 7
F-007
Lint loop
Same lint error 3 iterations
F-008
Type-check loop
Same tc error 3 iterations
F-009
Token budget overrun
Used > 2× expected_token_budget
F-010
Tool-call format error loop
JSON/grammar parse failure 3 iterations
F-011
Skill misapplication
Skill fired but post-conditions failed
F-012
Memory contradiction
Episodic and procedural disagree on same task class
5.5 Circuit breakers
Breaker
Trips at
Action
Token per step
2× expected_token_budget
Pause; re-plan
Wall-clock per agent
30 min Doer / 10 min others
Pause; escalate
Retry count per step
5
Mark step failed; re-plan
Stuck Detector
Any §5.4 trigger F-004/F-007/F-008/F-010
Pause; KGR or escalate
Audit-write failure
1
Halt — audit must succeed
Sandbox crash
3
Restart sandbox; if persists, escalate
Ticket wall-clock
4 hours
Pause; escalate to human
5.6 Kill-switches
Hard stops, separate from circuit breakers. A kill-switch ends agent execution immediately and writes a final audit event.
	•	Human kill via HITL UI (per ticket or system-wide)
	•	Audit pipeline failure (Postgres unreachable for >30s)
	•	Inference pool failure (no model available for required role)
	•	Sandbox pool exhaustion (no capacity after 60s wait)
6. Inference harness (#3)
Local model pool on Mac Studio M4 Max 128GB. Multi-model resident; hot-swap when RAM is tight.
6.1 Model assignment
Role
Default model class
Quant
RAM
Planner / Architect
Reasoning model (DeepSeek R1 Distill 32B / Qwen 3 reasoning)
Q4_K_M
20 GB
Doer
Code specialist (Qwen 2.5 / 3 Coder 32B)
Q4_K_M
20 GB
Verifier
Mid instruct (Qwen 2.5 14B)
Q5_K_M
10 GB
Understander
Same as Verifier (shared)
—
—
Tester
Code specialist 14B
Q5_K_M
10 GB
Grounder
Tiny instruct (Qwen 2.5 3B)
Q8
4 GB
Total resident
—
—
≈64 GB (leaves headroom)
6.2 Inference servers
Server
Pros
Cons
Verdict
llama.cpp (Metal)
Fast on Mac; mature; GGUF support
Single-tenant; weaker prefix caching
Default for narrow roles
MLX-LM (Apple)
Apple-native; best Apple Silicon perf for newer models
MLX format limits model availability
Default for Doer if MLX build available
LM Studio
Easy ops; OpenAI-compat API
Less control over batching
Dev/local prototyping only
6.3 Prefix cache (critical)
Prefix caching turns the static prompt prefix from a per-call cost into a one-time cost. Without it, throughput collapses on local hardware.
	•	Stable prompt order: [system] → [agent-rules.md] → [session_memory] → [skill_match] → [code_bundle] → [working_state] → [user_step]
	•	First six layers are prefix-cacheable; only working_state + user_step change between calls
	•	Inference server must be configured with prefix-cache enabled (llama.cpp: --cache-reuse 256, MLX: built-in)
	•	Cache hit rate target: ≥70% by end of P2
6.4 Sampling defaults
Role
Temperature
Top-p
Repetition penalty
Planner
0.3
0.9
1.0
Verifier
0.0
—
—
Doer
0.2
0.95
1.05
Tester
0.1
0.9
1.0
Architect
0.0
—
—
Understander
0.3
0.9
1.0
Grounder
0.0
—
—
6.5 Grammar-constrained decoding
Local models are noisy. Every structured output uses grammar-constrained decoding so output failures are impossible by construction.
Output
Grammar
Planner candidate plans
JSON schema → GBNF (steps[], each with tool/inputs/expected/criteria/depends_on)
Verifier verdict
JSON: {verdict, issues, revised_plan?}
Doer udiff edit
GBNF matching unified-diff format
Doer whole-file edit
Fenced code block with explicit path header
Tester test plan
JSON: {tests, coverage_target}
Architect review
JSON: {decision, comments[], mr_title, mr_body}
6.6 Hot-swap
If a role needs a model not currently resident, the pool unloads the least-recently-used non-pinned model and loads the requested one. Pinned models (Planner, Doer) stay resident always.
7. Memory harness (#4)
7.1 Six memory types, one client
Type
Backend
Written by
Read by
Code context
AiForgeMemory (Neo4j)
AiForgeMemory ingest
All agents
Episodic
Postgres + pgvector
Learner
Understander, Planner, Verifier
Procedural
Postgres + pgvector
Learner
Planner, Doer, Tester
Skills
Files (.aiforge/skills/)
Learner (after eval gate)
Planner first; Doer dispatch
Session memory
File per ticket (markdown)
Compactor (forked subagent)
All agents (anchored)
Audit
Postgres (append-only)
Auditor middleware
Learner, Web UI, ops
Prompts
Files (.aiforge/prompts/, versioned)
Engineers (PR-reviewed)
All agents
7.2 Postgres schemas
Monthly partitions on all tables. pgvector for embeddings.
episodic_outcomes
id              uuid primary key
ticket_id       text
stage           text         -- understand|plan|verify|ground|execute|review
agent_role      text
outcome         text         -- success|failure|escalated
summary         text
embedding       vector(1024)
artifacts       jsonb
hitl_weight     int default 1
created_at      timestamptz
procedural_patterns
id              uuid primary key
agent_role      text
task_class      text
tool_sequence   jsonb
preconditions   jsonb
success_count   int
failure_count   int
last_used_at    timestamptz
embedding       vector(1024)
skill_ref       text         -- pointer to .aiforge/skills/<name>/
audit_events
id              bigserial primary key
ticket_id       text
agent_role      text
event_type      text         -- before_model|after_model|before_tool|after_tool|
                              -- compaction|skill_fired|breaker_trip|kill_switch
payload         jsonb        -- redacted via deny-list
duration_ms     int
status          text
trace_id        text
created_at      timestamptz
step_traces
id              uuid primary key
ticket_id       text
agent_role      text
step_index      int
plan_step_id    text
input_context   jsonb
output          jsonb
tools_used      text[]
tokens_in       int
tokens_out      int
prompt_version  text
status          text
created_at      timestamptz
7.3 On-demand expansion
The Unified Memory Client returns summaries by default. Each carries a memory_id; agents call memory.expand(id) only when they need full content. Caps episodic context at ~16k tokens regardless of ticket complexity.
7.4 Skill tree
Self-evolving SOPs at .aiforge/skills/<name>/.
.aiforge/skills/<name>/
├── SKILL.md     # description, when_to_use, parameters (embedded for retrieval)
└── skill.py     # parametrised executable using @skill decorator
Lifecycle: procedural_pattern with success ≥3 and rate >80% → distillation subagent → eval gate (must pass on ≥3 representative tickets) → promoted to skill. Demotion: <70% over last 10 invocations → quarantined for human review.
7.5 Session memory file
Background-maintained markdown file per ticket. A forked subagent updates it when (token_growth ≥8k AND tool_calls ≥5).
# Session memory — TKT-2026-0001

## Current State
…

## Plan
…

## Files touched
…

## Errors & Corrections
…

## Worklog
…
7.6 Prompt registry
Prompts are code. Every change is a PR with eval-suite results. Active version recorded in step_traces.prompt_version.
.aiforge/prompts/
├── planner/
│   ├── system.v3.md          # active
│   ├── system.v2.md          # archived
│   └── CHANGELOG.md
├── doer/
│   ├── system.java.v2.md
│   ├── system.python.v2.md
│   └── system.typescript.v2.md
├── verifier/
│   └── system.v1.md
├── compactor/
│   └── compact-instructions.v1.md
└── ...
A/B testing: Learner can route 10% of tickets to a candidate prompt version. After 20 tickets, statistical significance test on success rate. Significant winner promoted; loser archived.
8. Archetypes (the agents)
Each archetype is an ADK class (LlmAgent or LoopAgent subclass) with role-specific prompt template, tool subset, sampling profile, and grammar.
8.1 Understander
	•	Subclass of LlmAgent; read-only tools (code_context, memory.expand, fs.read)
	•	Output: Understanding artifact (problem, knowns, unknowns, risks, ambiguities)
	•	If confidence < threshold, surfaces clarifying questions to HITL
8.2 Planner — hardened
Six layers of defence (carried over from v0.3, all preserved).
	•	§1 Skill-tree-first: query the tree; if match ≥0.8 confidence, plan = invoke skill
	•	§2 Multi-candidate: N=3 plans with different seeds; self-consistency on first step
	•	§3 Schema-constrained output (GBNF)
	•	§4 Plan grounding via Grounder agent
	•	§5 Prospective verification via Verifier (PreFlect)
	•	§6 Decomposition depth limit: max 7 steps; longer plans force task-split
	•	§7 Plan budget: declares expected_token_budget; runtime enforces 1.5× soft, 2× hard
8.3 Verifier
PreFlect-style critic. Receives plan(s) + top-5 episodic failures for similar tickets + top-3 known failure modes. Output: pass / repair-with-suggestions / reject.
8.4 Grounder
Validates every plan reference resolves before execution: tool registry, file paths via AiForgeMemory, symbols via Symbol_v2 nodes, imports via IMPORTS graph + package manifest. Mostly rule-based; uses tiny model for ambiguous cases.
8.5 Doer — hardened
CRITIC loop with five mechanisms layered.
Generate (grammar-constrained udiff)
   ↓
Parse → static checks:
  • Diff context lines hash-match target file?
  • New imports resolve via AiForgeMemory?
  • New symbols don't shadow existing ones?
   ↓
Apply to working tree
   ↓
Run lint (auto-fix safe issues)
   ↓
Run type-check (mypy/tsc/javac)
   ↓
Run failing-test set
   ↓
Test-name oracle: did Doer echo correct failing-test names?
   ↓
CRITIC self-check: did the diff change the failing-test set?
   ↓
All green → atomic git commit (Co-authored-by trailer)
Any red  → next iteration with structured gap object
8.6 Tester
LoopAgent. Writes failing tests FIRST (TDD). Verifies coverage ≥80% post-execute. Uses code-as-action for test generation.
8.7 Architect
Read-only review. Compares diff to Understanding + Plan. Creates MR if approved (and only then).
8.8 Coordinator
Optional. Used when a ticket needs Researcher + Doer in parallel. Wraps each archetype as a managed agent (smolagents pattern). Each managed agent has its own memory context — no cross-contamination.
8.9 Learner
Registered as ADK after_model + after_tool callback. Online: writes step_trace, updates episodic + procedural, fires skill-distillation if threshold crossed. Offline (weekly, Mac Studio): cluster failures, distil patterns, promote skills via eval gate, resolve contradictions, generate weekly Learner report.
9. Eval harness (#5)
Single source of truth for whether the system is getting better or worse. Includes regression testing for prompt changes.
9.1 Layers
Layer
What
Run when
Unit
Isolated tests for each archetype, callback, tool
On every PR
Integration
Multi-archetype flows (Planner → Verifier → Grounder)
On every PR
End-to-end
Full ticket → MR on a hello-world repo fixture
Nightly
Load
10 tickets in parallel; measures throughput, prefix-cache hit rate, RAM ceiling
Weekly
Eval suite
20 fixed real tickets with golden traces
On every prompt/model change + weekly
Replay
Re-run past tickets against new prompt/model versions
On candidate promotion
Prompt A/B
10% traffic split, statistical significance on 20 tickets
Continuous (Learner-driven)
9.2 The 20-ticket eval suite
Hand-curated, balanced across:
	•	5 add-feature tickets (REST endpoint, DTO, service method)
	•	5 fix-bug tickets (failing test, null check, boundary case)
	•	3 refactor tickets (rename, extract method, split file)
	•	3 test-only tickets (add coverage, fix flaky)
	•	2 multi-file tickets (controller + service + test)
	•	2 ambiguous tickets (deliberately under-specified to test Understander's clarifying-question path)
9.3 Replay engine
Given a step_trace history from a past ticket, the replay engine re-runs the same steps against a candidate prompt or model version. Compares outputs to golden traces. Used for regression testing — promoting a new prompt version requires no eval-suite regressions.
9.4 Golden traces
For each eval ticket, the system stores the canonical step_trace from a known-good run. Future runs are compared against the golden — divergence flagged for review.
10. Sandbox harness (#6)
10.1 Docker pool
Pre-warmed Docker containers. One per active ticket. Container reuses across steps within the same ticket — saves the cold-start cost on every Doer iteration.
10.2 Image
FROM python:3.12-slim
RUN apt-get update && apt-get install -y \
    git curl ca-certificates build-essential \
    nodejs npm openjdk-21-jdk-headless ruff mypy \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest mypy ruff
WORKDIR /workspace
USER 1000:1000
10.3 Allowlist
additional_authorized_imports policy is per-language and per-task-class. Default deny; explicit allowlist.
# .aiforge/sandbox/allowlist.yaml
defaults:
  python:
    - os.path
    - pathlib
    - json
    - typing
    - pytest
  java:
    - java.util.*
    - java.io.*
    - org.springframework.*
per_task_class:
  add-rest-endpoint:
    java:
      - jakarta.persistence.*
      - lombok.*
  add-db-migration:
    python:
      - alembic.*
10.4 Mounts
	•	Working tree (read-write, scoped to repo path)
	•	.aiforge/skills (read-only)
	•	AiForgeMemory cache directory (read-only)
	•	No host network beyond explicit allowed domains (web search, Stack Overflow, MCP servers)
11. Observability harness (#7)
11.1 Stack
Layer
Tooling
Audit (append-only)
Postgres audit_events table
Traces
OpenTelemetry → local Jaeger (dev) / Tempo (prod)
Metrics
Prometheus + Grafana
Logs
Structured JSON to stdout → captured by docker-compose logging driver
11.2 Auditor middleware
ADK callbacks (before_model, after_model, before_tool, after_tool) call into the Auditor. Pre-phase records intent; post-phase records outcome. Failure to record an event blocks the action — audit is a hard dependency.
11.3 Redaction
Deny-list of keys (passwords, tokens, secrets, credit-card numbers, customer PII). Applied to every payload before write. Implemented as a single utility used by every audit-write site.
11.4 Dashboards
	•	Per-ticket: stages timeline, token usage, tool calls, errors
	•	Per-archetype: throughput, p50/p95 latency, retry rate, circuit-breaker trips
	•	Inference: prefix-cache hit rate per role, model RAM utilisation, hot-swap frequency
	•	System: eval-suite pass rate over time, HITL intervention rate, mean time ticket-to-MR
12. HITL harness (#8)
Next.js (TypeScript) Web UI. Sole human entry point. No public API; UI writes directly to the Orchestrator's internal store.
12.1 Surfaces
Page
What
Ticket creator
Form: title / body / repo / branch_base / labels — writes a ticket in created state
My queue
Tickets currently waiting on me (approval, clarification)
Approval queue
Tickets at mr_open state with risk score, diff, plan + understanding side-by-side
Replay viewer
Step-by-step walk through any past ticket
Skill browser
View / quarantine / edit skills; promotion / demotion log
Prompt registry
Active versions, A/B status, change history
Audit explorer
Query past actions / costs / patterns
Dashboards
Embedded Grafana panels
12.2 Approval flow
	•	Reviewer sees diff + plan + understanding
	•	Approve → Orchestrator promotes mr_open → merged
	•	Reject → ticket → abandoned, written to episodic with hitl_weight=10
	•	Request changes → ticket → executing with comment as new context
13. Repo layout
aiforge_agents/                          # ONE REPO
├── pyproject.toml                       # uv workspace
├── pnpm-workspace.yaml                  # for hitl_web
├── README.md
├── docs/SPEC.md
│
├── aiforge_agents/                      # Python package
│   ├── orchestrator/                    # HARNESS 1
│   │   ├── tickets.py
│   │   ├── org_chart.py
│   │   ├── governance.py
│   │   └── mr_gate.py
│   │
│   ├── runtime/                         # HARNESS 2 (ADK-based)
│   │   ├── agent_runner.py
│   │   ├── callbacks/
│   │   │   ├── compactor.py
│   │   │   ├── auditor.py
│   │   │   ├── circuit_breakers.py
│   │   │   ├── stuck_detector.py
│   │   │   ├── failure_taxonomy.py
│   │   │   └── learner_hook.py
│   │   ├── tool_registry.py
│   │   └── kill_switches.py
│   │
│   ├── inference/                       # HARNESS 3
│   │   ├── pool.py
│   │   ├── prefix_cache.py
│   │   ├── llamacpp_client.py
│   │   ├── mlx_client.py
│   │   ├── sampling.py
│   │   └── grammars/
│   │       ├── plan.gbnf
│   │       ├── verify.gbnf
│   │       └── udiff.gbnf
│   │
│   ├── memory/                          # HARNESS 4
│   │   ├── unified.py
│   │   ├── code_context.py
│   │   ├── episodic.py
│   │   ├── procedural.py
│   │   ├── session_mem.py
│   │   ├── audit_store.py
│   │   └── prompt_registry.py
│   │
│   ├── archetypes/
│   │   ├── understander.py
│   │   ├── planner.py
│   │   ├── verifier.py
│   │   ├── grounder.py
│   │   ├── doer.py
│   │   ├── tester.py
│   │   ├── architect.py
│   │   ├── coordinator.py
│   │   └── learner.py
│   │
│   ├── skills/
│   │   ├── runtime.py
│   │   ├── distiller.py
│   │   └── eval_gate.py
│   │
│   ├── sandbox/                         # HARNESS 6
│   │   ├── docker_pool.py
│   │   ├── policies.py
│   │   └── warm_container.py
│   │
│   ├── observability/                   # HARNESS 7
│   │   ├── traces.py
│   │   ├── metrics.py
│   │   ├── cost.py
│   │   └── redaction.py
│   │
│   └── tools/
│       ├── shell.py
│       ├── fs.py
│       ├── git.py
│       ├── http.py
│       ├── code_review_graph.py
│       └── knowledge_gap_resolver.py
│
├── evals/                               # HARNESS 5
│   ├── tickets/                         # 20 fixed
│   ├── replay.py
│   ├── golden_traces/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   ├── load/
│   └── prompt_ab.py
│
├── hitl_web/                            # HARNESS 8
│   ├── app/
│   ├── components/
│   └── package.json
│
├── .aiforge/                            # runtime config
│   ├── agent-rules.md
│   ├── compact-instructions.md
│   ├── failure-taxonomy.yaml
│   ├── prompts/
│   ├── skills/
│   ├── runbooks/
│   └── sandbox/
│       └── allowlist.yaml
│
├── migrations/                          # Postgres schemas
└── docker-compose.yml                   # Postgres + Neo4j (dev)
14. Phase plan
Phase
Scope
Duration
Exit criteria
P0
AiForgeMemory API audit + ADK bring-up + Mac Studio model bring-up + prefix-cache validation
1 week
All chosen models running on Mac Studio with measurable prefix-cache hit rate; ADK Runner working with one toy LlmAgent end-to-end
P1
Harnesses 2 (Runtime: callbacks, tool registry, breakers, taxonomy) + 3 (Inference) + 4 (Memory) + Understander + Planner + Verifier + Grounder
3 weeks
Smoke test: ticket → plan generated, verified, grounded, schema-valid
P2
Harness 6 (Sandbox) + Doer (CRITIC loop) + Tester + Architect + KGR + harness 1 (Orchestrator) + harness 7 (Observability)
3 weeks
End-to-end MR raised on real ticket; F-001..F-012 detectors firing correctly; audit pipeline complete
P3
Learner + skill tree (distillation + eval gate) + harness 5 (Eval): 20 tickets + replay + golden traces + prompt A/B
2 weeks
First skill auto-promoted; replay reproduces past tickets; eval suite green
P4
Harness 8 (HITL Web UI) + dashboards + docs + hardening
2 weeks
HITL corrections visibly improve next-ticket performance; all 8 harnesses production-ready
15. Success criteria
Phase end
Metric
Target
P0
Mac Studio prefix-cache hit rate baseline
Measurable
P0
ADK toy agent end-to-end
Working
P1
Plan grounding success
≥95% of generated plans pass Grounder
P1
Schema-constrained output failures
0% (grammar-enforced)
P2
Ticket success rate vs Hermes baseline (when AIForgeCrew was using Hermes)
Parity (within 5%)
P2
Hallucinated-import block rate
100%
P3
Ticket success rate after 4 weeks of learning
≥10% improvement OR ≥15% token cost reduction
P3
Skills auto-promoted in first 4 weeks
≥3
P3
Stuck Detector trips per ticket
Trending down
P4
HITL intervention rate
Trending down
P4
Mean ticket-open to MR
Flat or trending down
16. Risks and mitigations
Risk
Likelihood
Mitigation
Local Planner not strong enough for complex tickets
High
Multi-candidate + Verifier + skill-first; complex tickets escalate
Local Doer hallucinates imports/APIs
High
Hallucinated-import killer + symbol grounding pre-apply
Mac Studio thermal throttling under sustained load
Medium
Wall-clock breakers; staged inference; monitor in §11 dashboards
Prefix-cache invalidation cascades on prompt edits
Medium
Stable prompt order; bump only changed segment's cache key
Grammar-constrained decoding slows generation 2-3×
Medium
Worth it; budget extra latency; bench in P0
ADK API instability across versions
Medium
Pin ADK version; wrap callbacks in our adapter so version bumps are surgical
Sandbox per-step latency
Medium
Warm-container reuse per ticket
AiForgeMemory query latency (5-8s)
Medium
Cache ContextBundle per ticket
Catastrophic forgetting in Learner
Medium
Eval gate on skill promotion; weekly contradiction resolution
Skill tree bloat
Medium
Demotion below 70%; quarantine for human review
KGR pulls licensed code
Low-Medium
Strip code blocks >15 lines
Postgres bottleneck
Low
Monthly partitions; migration path to ClickHouse
No external orchestrator means new failure modes (queue, scheduling)
Medium
Start single-tenant; one ticket at a time in v1; multi-tenant in v2
17. Open questions
	•	Final model selection per role — bench candidates in P0 on 5 representative tickets
	•	Inference server: llama.cpp vs MLX-LM — bench in P0
	•	Grammar-constrained decoding: GBNF (llama.cpp) vs Outlines vs MLX guided — pick after P0 bench
	•	AiForgeMemory roles available beyond doer (planner/tester/reviewer presumed)
	•	Single-ticket vs multi-ticket execution in v1 — recommend single, but confirm
	•	SolAgents adapter scope — separate doc
	•	HITL Web UI auth — local-only password / SSO / open in dev?
18. Glossary
Term
Definition
ADK
Google Agent Development Kit — Python framework providing LlmAgent, LoopAgent, Runner, callbacks
Harness
Self-contained subsystem with its own scope and interfaces (8 in v0.4)
Archetype
An ADK-based agent class (Planner, Doer, etc.)
AiForgeMemory
External Neo4j-backed code intelligence service; provides ContextBundle
CRITIC
Tool-interactive critiquing — agent issues verification actions and uses tool output as evidence
PreFlect
Prospective reflection — critique the plan before execution
Verifier
PreFlect-style critic agent
Grounder
Validates plan references resolve in AiForgeMemory before execution
Stuck Detector
Heuristic component for no-progress patterns
Failure taxonomy
F-001..F-012 — known failure modes with detectors
Prefix cache
Inference-server feature reusing KV-cache for stable prompt prefixes
Grammar-constrained decoding
Forces model output to a formal grammar (GBNF, JSON schema)
udiff
Unified diff format
Skill tree
.aiforge/skills/ — self-evolving runnable SOPs
Anchored memory
Pinned context surviving compaction
Code-as-action
smolagents pattern — Python tool calls
Three-tier compaction
microcompact / full / session memory file
