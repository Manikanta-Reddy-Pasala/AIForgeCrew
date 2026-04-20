# AIForgeCrew

**Autonomous AI Development Team — Design Document v3.0**
**Author:** Manikanta Reddy Pasala | **Date:** April 2026
**Repo:** `github.com/Manikanta-Reddy-Pasala/AIForgeCrew`

> **Status:** Partially superseded. Sections §4 (TDD lifecycle), §5 (tool stack), §6 (memory), §7 (RAG) are replaced by
> `docs/superpowers/specs/2026-04-21-autonomous-memory-orchestration-design.md` (pipeline v4.1).
> Sections §1–§3 org and §8 security remain current.
> Graphify code-KG is wired as an MCP tool (`search_graph`) alongside T4 code RAG — see §13 of the v4.1 spec.

---

## 1. One-Liner

Human devs create a ticket in Paperclip → AI agents plan, write tests first (TDD), develop code to pass, review, and raise MR — all updates on the same ticket.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     PAPERCLIP (Orchestrator)                     │
│       Org Chart · Tickets · Budgets · Governance · Audit Log     │
├──────────┬────────────┬────────────┬────────────┬────────────────┤
│  👤 Human│  🧠 EM      │  🧪 Tester  │  👨‍💻 Sr Dev  │  🏛 Sr Architect│
│  (Creates│  (Planning) │  (Tests    │  (Code to  │  (Review)      │
│  ticket) │  Cloud LLM  │  FIRST)    │  pass)     │  Local LLM     │
│          │             │  Local LLM │  Local LLM │                │
└──────────┴─────┬──────┴─────┬──────┴─────┬──────┴───────┬────────┘
                 │            │            │              │
                 │     ALL UPDATES GO TO SAME TICKET      │
                 │                                        │
       ┌─────────────────────────────────────────────────────────┐
       │              Hermes Agent (Runtime per agent)            │
       └────┬──────────┬──────────────┬──────────────┬───────────┘
            │          │              │              │
     ┌──────┴───┐ ┌────┴─────┐ ┌─────┴──────┐ ┌────┴──────┐
     │  Mem0    │ │ code-    │ │  Git MCP   │ │  RAG      │
     │ Agent +  │ │ review-  │ │            │ │ (Project  │
     │ Project  │ │ graph    │ │            │ │  Docs)    │
     └──────────┘ └──────────┘ └────────────┘ └───────────┘
            │          │              │              │
       ┌────┴──────────┴──────────────┴──────────────┴───────────┐
       │              Observability & Audit Layer                 │
       │        All traces threaded under original ticket ID      │
       └─────────────────────────────────────────────────────────┘
```

---

## 3. Agent Roles — Skills, Limits & Contracts

### 3.1 Engineering Manager (EM)

| Attribute | Value |
|-----------|-------|
| **Reports to** | CEO (human) |
| **Model** | TBD — Cloud (Claude/GPT/Gemini) |
| **Skills** | Task decomposition, acceptance criteria, test scenario definition, effort estimation |
| **Limitations** | Cannot write code. Cannot execute commands. Cannot access Git. Cannot create MR. |
| **Contract** | IN: human ticket. OUT: subtasks + acceptance criteria + test scenarios → comments on SAME ticket, assigns to Tester. |
| **Memory** | Project goals, sprint context, velocity, past planning decisions |

### 3.2 Tester (QA) — RUNS FIRST (TDD)

| Attribute | Value |
|-----------|-------|
| **Reports to** | EM |
| **Model** | TBD — Local |
| **Skills** | Unit test writing, integration test writing, test execution, coverage reporting, test scenario design |
| **Limitations** | Cannot modify production code (`src/`). Cannot create MR. Cannot approve. |
| **Contract** | IN: acceptance criteria + test scenarios from EM. OUT: failing test files committed to branch + "tests ready, all failing as expected" comment on ticket. After dev: re-run tests, report pass/fail + coverage on ticket. |
| **Memory** | Test strategies, known flaky areas, coverage gaps, regression patterns |
| **Secrets access** | READ: `.env.test`, `config/test/` — needed to run tests. DENY: `.env`, `.env.prod`, `config/prod/`, `secrets/` |

### 3.3 Sr Developer

| Attribute | Value |
|-----------|-------|
| **Reports to** | EM |
| **Model** | TBD — Local |
| **Skills** | Code generation, refactoring, bug fixing, making failing tests pass |
| **Limitations** | Cannot create MR. Cannot approve. Cannot modify test files. Cannot assign tickets. Cannot access secrets. |
| **Contract** | IN: failing tests + acceptance criteria. OUT: production code that makes ALL tests pass → committed to branch + "all tests passing" comment on ticket. **Every code change must have corresponding unit tests already written by Tester.** |
| **Memory** | Codebase patterns, past fixes, project conventions, Hermes-learned skills |

### 3.4 Sr Software Architect (Reviewer)

| Attribute | Value |
|-----------|-------|
| **Reports to** | EM |
| **Model** | TBD — Local (reasoning-optimized) |
| **Skills** | Code review, security audit, architecture compliance, SOLID/DRY, test quality review |
| **Limitations** | Cannot write code. Cannot execute code. Cannot modify files. Read-only access. |
| **Contract** | IN: branch with passing tests. OUT: approve → MR created on ticket, OR reject → review comments with file:line references on ticket. **Must verify unit test coverage ≥80% before approving.** |
| **Memory** | Review history, recurring issues, security patterns, architecture decisions |

---

## 4. Ticket Lifecycle (TDD)

**Key rule: ONE ticket. All agents comment on the same ticket. No sub-tickets.**

```
HUMAN                        PAPERCLIP (same ticket)          GIT
  │                              │                             │
  ├── Creates ticket ───────────►│ TICKET-123                  │
  │   assigns to EM              │                             │
  │                              │                             │
  │                         [EM] comments: subtasks +          │
  │                              acceptance criteria +         │
  │                              test scenarios                │
  │                         [EM] assigns to Tester             │
  │                              │                             │
  │                      ┌── TDD PHASE 1: TESTS FIRST ──┐     │
  │                      │                               │     │
  │                      │  [Tester] writes unit tests   │     │
  │                      │  [Tester] writes integ tests  │     │
  │                      │  [Tester] commits ────────────┼────►├─ branch: feat/TICKET-123
  │                      │  [Tester] runs tests          │     │
  │                      │  [Tester] comments:           │     │
  │                      │    "12 tests written,         │     │
  │                      │     all failing as expected"  │     │
  │                      │  [Tester] assigns to Sr Dev   │     │
  │                      └───────────────────────────────┘     │
  │                              │                             │
  │                      ┌── TDD PHASE 2: MAKE IT PASS ──┐    │
  │                      │                                │    │
  │                      │  [Sr Dev] reads failing tests  │    │
  │                      │  [Sr Dev] writes prod code     │    │
  │                      │  [Sr Dev] commits ─────────────┼───►├─ push to feat/TICKET-123
  │                      │  [Sr Dev] comments:            │    │
  │                      │    "code ready for test run"   │    │
  │                      └────────────────────────────────┘    │
  │                              │                             │
  │                      ┌── TDD PHASE 3: VERIFY ─────────┐   │
  │                      │                                 │   │
  │                      │  [Tester] runs all tests        │   │
  │                      │                                 │   │
  │                      │  ALL PASS + coverage ≥80%?      │   │
  │                      │    YES → comments: "✅ 12/12    │   │
  │                      │           pass, 87% coverage"   │   │
  │                      │           assigns to Architect  │   │
  │                      │                                 │   │
  │                      │    NO → comments: "❌ 3/12      │   │
  │                      │          fail" + details        │   │
  │                      │          assigns back to Sr Dev │   │
  │                      │          (retry ≤3)             │   │
  │                      └─────────────────────────────────┘   │
  │                              │                             │
  │                      ┌── REVIEW ──────────────────────┐    │
  │                      │                                │    │
  │                      │  [Sr Architect] reviews code   │    │
  │                      │  [Sr Architect] reviews tests  │    │
  │                      │                                │    │
  │                      │  APPROVE → comments: "✅ LGTM" │    │
  │                      │           creates MR ──────────┼───►├─ MR created
  │                      │                                │    │
  │                      │  REJECT → comments: review     │    │
  │                      │           notes + file:line     │    │
  │                      │           assigns to Sr Dev     │    │
  │                      │           (retry ≤3)            │    │
  │                      └────────────────────────────────┘    │
  │                              │                             │
  ◄── Notified: MR ready ───────┤                             │
      (human reviews + merges)   │                             │
```

### TDD Enforcement Rules

- **No code without tests:** Sr Dev cannot commit to `src/` unless corresponding test files exist in `tests/`
- **Tests must fail first:** Tester verifies all new tests fail before Sr Dev starts coding
- **Minimum coverage:** Sr Architect blocks MR if unit test coverage < 80%
- **Test file naming:** `tests/unit/test_<module>.py` or `tests/<module>.test.ts` mirroring `src/` structure

---

## 5. Tool Stack & Contracts

### 5.1 Tools

| Tool | Contract |
|------|----------|
| **Paperclip** | IN: agent defs + org chart. OUT: ticket routing, heartbeat, audit trail, cost tracking. |
| **Hermes Agent** | IN: task + prompt + tools. OUT: tool calls, code exec, skill creation, memory writes. |
| **Mem0** | IN: agent interaction. OUT: compressed memories (<8K), entity graph, cross-session recall. |
| **code-review-graph** | IN: file change / query. OUT: blast radius, dependency chain, structural context. |
| **RAG** | IN: natural language query. OUT: relevant chunks from project docs, ADRs, API specs. |
| **Git MCP** | IN: agent action. OUT: Git op result. Scoped per agent permissions. |
| **Local inference** | IN: OpenAI-compatible request. OUT: completion. |
| **Cloud API** | IN: planning request (EM only). OUT: task decomposition. |

### 5.2 Tool Permissions

| Tool / MCP | EM | Tester | Sr Dev | Sr Architect |
|---|:---:|:---:|:---:|:---:|
| Cloud LLM | ✅ | ❌ | ❌ | ❌ |
| Local LLM | ❌ | ✅ | ✅ | ✅ |
| Git: branch/commit/push | ❌ | ✅ (tests/) | ✅ (src/) | ❌ |
| Git: create MR | ❌ | ❌ | ❌ | ✅ |
| Git: read repo | ❌ | ✅ | ✅ | ✅ (read-only) |
| code-review-graph | ❌ | ✅ | ✅ | ✅ |
| Mem0: own memory R/W | ✅ | ✅ | ✅ | ✅ |
| Mem0: project memory read | ✅ | ✅ | ✅ | ✅ |
| Mem0: project memory write | ✅ | ❌ | ❌ | ✅ |
| RAG: query docs | ✅ | ✅ | ✅ | ✅ |
| Hermes: code execution | ❌ | ✅ | ✅ | ❌ |
| Hermes: skill creation | ❌ | ✅ | ✅ | ✅ |
| Ticket: comment | ✅ | ✅ | ✅ | ✅ |
| Ticket: assign | ✅ | ✅ | ❌ | ❌ |
| File: write src/ | ❌ | ❌ | ✅ | ❌ |
| File: write tests/ | ❌ | ✅ | ❌ | ❌ |
| File: read src/ | ❌ | ✅ | ✅ | ✅ |
| File: read tests/ | ❌ | ✅ | ✅ | ✅ |
| `.env.test`, `config/test/` | ❌ | ✅ (read) | ❌ | ❌ |
| `.env`, `.env.prod`, secrets/ | ❌ | ❌ | ❌ | ❌ |
| CI/CD configs | ❌ | ❌ | ❌ | ❌ |
| Network / external APIs | ❌ | ❌ | ❌ | ❌ |

---

## 6. Memory Architecture

### 6.1 Two-Tier Memory

```
┌─────────────────────────────────────────────────┐
│                    Mem0                          │
│                                                 │
│  ┌─────────────────────────────────────────┐    │
│  │         PROJECT MEMORY (shared)         │    │
│  │  Write: EM + Sr Architect only          │    │
│  │  Read: all agents                       │    │
│  │                                         │    │
│  │  Architecture decisions, coding         │    │
│  │  standards, known tech debt,            │    │
│  │  sprint context, API contracts          │    │
│  └─────────────────────────────────────────┘    │
│                                                 │
│  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐        │
│  │ EM   │  │ QA   │  │ Dev  │  │ Arch │        │
│  │ own  │  │ own  │  │ own  │  │ own  │        │
│  │memory│  │memory│  │memory│  │memory│        │
│  └──────┘  └──────┘  └──────┘  └──────┘        │
│                                                 │
│  Token budget: <8K per agent call               │
└─────────────────────────────────────────────────┘
```

---

## 7. RAG — Project Documentation

| Need | Solution |
|------|----------|
| "What happened last sprint?" | Mem0 (memory) |
| "What does our API spec say?" | RAG (docs) |
| "What files break if I change X?" | code-review-graph |

**What gets indexed:** API specs, ADRs, README, coding standards, runbooks, DB schemas, security policies.

**Stack:** Local embedding model (nomic-embed-text), Mem0 vector store or ChromaDB, markdown-aware chunking. Re-index on push to main.

---

## 8. Security

### 8.1 Principles

1. **Zero trust between agents**
2. **Least privilege** (see Section 5.2)
3. **Prod secrets blocked for ALL agents** — `.env`, `.env.prod`, `secrets/`, `config/prod/`
4. **Test secrets available to Tester** — `.env.test`, `config/test/` (read-only)
5. **No network access** for local agents
6. **No merge authority** — only humans merge
7. **All inference local** — no source code leaves machine (EM planning calls contain ticket text only, no code)

### 8.2 Threat Controls

| Threat | Control |
|--------|---------|
| Malicious code | Sr Architect review + human merge gate |
| Prod secrets access | Blocked for all agents. Test secrets read-only for Tester only. |
| Code exfiltration | No network tool for any local agent |
| Prompt injection via ticket | EM sanitizes before passing to agents |
| Runaway spend | Per-agent budget caps + circuit breaker |
| CI/CD tampering | CI configs excluded from all writable paths |
| Agent impersonation | Isolated Hermes workspace per agent |
| Model tampering | Verified checksums, no auto-update |
| Memory poisoning | Project memory writes: EM + Architect only |

### 8.3 File System Sandboxing

```
Tester:
  WRITE: tests/**
  READ:  src/**, tests/**, docs/**, .env.test, config/test/
  DENY:  .env, .env.prod, secrets/**, config/prod/**, .github/**

Sr Developer:
  WRITE: src/**
  READ:  src/**, tests/**, docs/**
  DENY:  .env*, secrets/**, config/prod/**, config/test/**, .github/**

Sr Architect:
  WRITE: NONE
  READ:  src/**, tests/**, docs/**, CI configs
  DENY:  .env*, secrets/**

EM:
  WRITE: NONE
  READ:  NONE (ticket text only)
```

---

## 9. Observability & Audit

### 9.1 Single-Ticket Threading

**All agent activity is threaded under the original human-created ticket.** One ticket = one complete lifecycle. No sub-tickets, no separate conversations.

```
TICKET-123: "Add user authentication endpoint"
  │
  ├─ [Human]  Created ticket. Assigned to EM.
  ├─ [EM]     Subtasks: 1) JWT middleware 2) login endpoint 3) tests
  ├─ [EM]     Acceptance criteria: ...
  ├─ [EM]     Assigned to Tester
  ├─ [Tester] Wrote 14 unit tests. All failing. Branch: feat/TICKET-123
  ├─ [Tester] Assigned to Sr Dev
  ├─ [Sr Dev] Implemented JWT middleware + login endpoint. Committed.
  ├─ [Sr Dev] "Code ready for test run"
  ├─ [Tester] Test results: 12/14 pass. 2 failures: test_token_expiry, test_invalid_cred
  ├─ [Tester] Assigned to Sr Dev
  ├─ [Sr Dev] Fixed token expiry logic. Committed.
  ├─ [Tester] Test results: 14/14 pass. Coverage: 91%.
  ├─ [Tester] Assigned to Sr Architect
  ├─ [Sr Arch] Review: LGTM. Coverage 91%. No security issues.
  ├─ [Sr Arch] MR #47 created.
  └─ [Human]  Merged MR #47. Ticket closed.
```

### 9.2 What We Track

| Layer | What | How |
|-------|------|-----|
| **Agent activity** | Every tool call, LLM request, decision | Paperclip audit log (per ticket) |
| **Token usage** | Tokens in/out per agent, per ticket | Paperclip cost dashboard |
| **Latency** | TTFT, generation time per agent call | Hermes + inference metrics |
| **Error rates** | Failed tool calls, retries | Hermes logs |
| **Test results** | Pass/fail counts, coverage % | Tester comments on ticket |
| **Git ops** | Branches, commits, MRs | Git MCP logs |
| **Ticket timing** | Time per stage (plan → test → dev → verify → review → MR) | Paperclip timestamps |
| **Cost** | Cloud spend, total per ticket | Paperclip budget |

### 9.3 Audit Rules

- **Append-only** — Paperclip log is immutable
- **Single thread** — all activity under one ticket ID
- **Full trace** — any MR traces back: MR → review → test results → code → failing tests → plan → ticket
- **Hermes checkpoints** — every 15 tool calls, auto-pause + self-assessment logged
- **Replay** — any ticket lifecycle replayable from log

---

## 10. Retry & Safety

| Mechanism | Rule |
|-----------|------|
| Max dev↔tester loops | 3, then escalate to human on ticket |
| Max dev↔reviewer loops | 3, then escalate to human on ticket |
| Token budget per task | Per-agent in Paperclip — kills on exceed |
| Circuit breaker | 3 consecutive failures → pause + alert human |
| Cost ceiling | Monthly per-agent budget |
| Human gate | **Agents cannot merge.** Human only. |
| Hermes checkpoint | Every 15 tool calls, auto-pause + self-check |
| Stale ticket timeout | No activity for 1 hour → alert human |
| Unit test requirement | MR blocked if coverage < 80% |

---

## 11. Repo Structure

```
AIForgeCrew/
├── README.md
├── DESIGN.md                  ← this document
├── LICENSE
├── docker-compose.yml
├── paperclip.config.yml
│
├── agents/
│   ├── em/
│   │   ├── system-prompt.md
│   │   ├── contract.md
│   │   └── permissions.yml
│   ├── tester/                ← listed before dev (TDD order)
│   │   ├── system-prompt.md
│   │   ├── contract.md
│   │   └── permissions.yml
│   ├── sr-developer/
│   │   ├── system-prompt.md
│   │   ├── contract.md
│   │   └── permissions.yml
│   └── sr-architect/
│       ├── system-prompt.md
│       ├── contract.md
│       └── permissions.yml
│
├── hermes/
│   ├── config.yml
│   └── skills/
│
├── memory/
│   ├── mem0-config.yml
│   ├── project-memory.yml
│   └── agent-schemas/
│
├── rag/
│   ├── index-config.yml
│   └── sources.yml
│
├── mcp/
│   ├── code-review-graph.json
│   ├── git-tools.json
│   └── rag-server.json
│
├── security/
│   ├── file-access-rules.yml
│   ├── blocked-paths.yml
│   └── model-checksums.yml
│
├── observability/
│   ├── dashboard-config.yml
│   └── alerts.yml
│
├── scripts/
│   ├── setup-models.sh
│   ├── start-servers.sh
│   ├── health-check.sh
│   └── verify-checksums.sh
│
└── docs/
    ├── hardware-guide.md
    ├── model-evaluation.md
    ├── security-policy.md
    └── troubleshooting.md
```

---

## 12. Phase Plan

| Phase | Scope | ETA |
|-------|-------|-----|
| **P0** | Buy M3 Ultra. Install models. Benchmark. | Week 1 |
| **P1** | Paperclip org chart + contracts + permissions. | Week 2 |
| **P2** | Hermes Agent instances. Skills + MCP tools. | Week 3 |
| **P3** | Mem0 two-tier memory. Test compression. | Week 4 |
| **P4** | code-review-graph + RAG. Wire as MCP. | Week 5 |
| **P5** | Git MCP. End-to-end single ticket (TDD flow). | Week 6 |
| **P6** | Security — file ACLs, checksums, sandbox. | Week 7 |
| **P7** | Observability — dashboards, alerts, audit. | Week 7 |
| **P8** | Retry logic, circuit breakers, TDD enforcement. | Week 8-9 |
| **P9** | Model evaluation per role. Finalize. | Week 10 |
| **P10** | Docs, blog post, demo video. | Week 11 |

---

## 13. Success Metrics

| Metric | Target |
|--------|--------|
| Ticket → MR (bug fix) | < 30 min |
| Ticket → MR (feature) | < 4 hours |
| Unit test coverage | ≥ 80% (enforced) |
| Tests written before code | 100% (TDD enforced) |
| Human intervention rate | < 20% of tickets |
| Token savings (code-review-graph) | > 50% |
| Monthly cloud cost (EM only) | < $50 |
| Audit completeness | 100% — every MR traceable |
| Security violations | 0 |

---

**Repo:** `github.com/Manikanta-Reddy-Pasala/AIForgeCrew`

*Stack: Paperclip · Hermes Agent · Mem0 · code-review-graph · RAG · Apple Silicon*
