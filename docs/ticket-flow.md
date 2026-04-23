# What happens when a ticket arrives

Ticket enters with `status=todo`. Within 60 s the graph-runner picks it up
and walks it through the LangGraph state machine on Mac Studio.

```
      POST /api/tickets  →  NUC Postgres (tickets)
                            status=todo
                                │
       (graph-runner cron, 60 s — launchd on Mac Studio)
                                │
                                ▼
                  ┌──────── supervisor_node ────────┐
                  │  rule-based routing (no LLM)    │
                  └────┬───────────────┬────────────┘
                       │ planner       │ doer
                       ▼               ▼
       ┌────── planner_node ──────┐   (only if ticket already has ## Files)
       │  smolagents CodeAgent    │
       │  tools:                  │
       │   · search_memory        │  (hybrid: vector + BM25 + rerank)
       │   · grep_repos           │  (~/codeRepo/*)
       │   · read_file            │
       │   · extract_signatures   │
       │   · write_plan           │  ← mutates ticket body (## Files, Plan,
       │   · create_child_ticket  │     Signatures, Compile pitfalls)
       │   · ticket_brief (MCP)   │     (via NUC graph-rag neighbourhood)
       │  model: qwen3.6-27b      │
       └──────────┬───────────────┘
                  │  ticket body enriched
                  ▼
       ┌────── doer_node ──────────┐
       │  smolagents ToolCallingAgent              │
       │  max_steps = 12                           │
       │  tools:                                   │
       │   · read_file / grep / list_dir           │
       │   · edit_block (scope-guarded)            │
       │   · run_compile (mvn -q -DskipTests)      │
       │   · run_shell (narrow allowlist)          │
       │  model: qwen3.6-35b-a3b@8bit              │
       │                                           │
       │  Checklist gate before final_answer:      │
       │    edit_block_ok ≥ N   (N = numbered      │
       │    acceptance items in ticket body)       │
       │    compile_green ≥ 1                      │
       └──────────┬────────────────────────────────┘
                  │ compile green + diff non-empty
                  ▼
            git commit → force-with-lease push →
            gh pr create (on aiforge/<ticket>)
                  │
                  ▼
       ┌────── feedback_node ──────┐
       │  single-shot LLM verdict on the diff       │
       │  prompt: ticket body + git diff HEAD~1     │
       │  must return JSON { verdict, reason,       │
       │                     fixlist }              │
       │  verdict ∈ {pass, fail, scope_violation}   │
       │  max_feedback_fails = 2 → doer retry      │
       │  model: qwen3.6-27b, enable_thinking:false│
       └──────────┬────────────────────────────────┘
              pass │                │ fail
                   ▼                ▼
           ┌── learner_node ──┐   back to doer
           │  LLM digest      │   (up to 2 retries
           │  → T1 memory     │    within the same
           │  in Postgres     │    graph invocation)
           │  model: 27b      │
           └────────┬─────────┘
                    │
                    ▼
            status=done, worktree cleaned
            memory digested, PR live
```

## Where each artifact lives

| Artifact | Host |
|---|---|
| Ticket row | NUC Postgres `tickets` |
| Plan (ticket body enrichment) | NUC Postgres |
| Worktree (`~/codeRepo/<repo>/.aiforge-worktrees/<ident>`) | **Mac Studio** |
| LLM calls | Mac Studio LM Studio :1234 (both roles) |
| Memory digest (T1) | NUC Postgres `memories` |
| Commit + branch + PR | GitHub |
| Graph-RAG context used by Planner | NUC Neo4j |

## Key safety gates

- **Scope guard** — `parse_allowed_files(body)` restricts Doer's `edit_block`
  to paths declared in `## Files`.
- **Worktree isolation** — every parent ticket gets a dedicated git
  worktree; ticket failures never touch the repo's main working tree.
- **Acceptance-item counter** — Doer must produce ≥ N `edit_block` calls
  for an N-item ticket. Premature `final_answer` rejected.
- **Verdict parser** — brace-depth scan + regex fallback; empty / non-JSON
  replies still get a usable verdict+reason instead of blocking.
- **force-with-lease push** — safe for agent-owned branches; prevents
  "branch behind" errors on retry.

## Status transitions

```
todo → in_progress → (done | blocked | cancelled)
```

- `done` on verdict=pass
- `blocked` on verdict=fail after retries, scope_violation, or loop_detect
- `cancelled` only by human PATCH
