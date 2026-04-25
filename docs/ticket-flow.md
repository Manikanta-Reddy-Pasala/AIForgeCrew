# Ticket Flow

End-to-end visual: from `curl POST /api/tickets` to PR-on-GitHub.

## High-level flow

```mermaid
flowchart TD
    A[User / Architect<br/>curl POST /api/tickets]:::ext --> B[NUC FastAPI :8799<br/>insert into Postgres]
    B --> C[adk_runner.service<br/>poll every 12s<br/>claim_next_any]
    C --> D[ADK SequentialAgent<br/>build session]
    D --> E[Planner agent<br/>direct LiteLLM]
    E -->|plan.md w/ checkboxes| F[LoopAgent max 4]
    F --> G[Doer agent<br/>GA loop + plan mode]
    G --> H[Feedback agent<br/>direct LiteLLM]
    H -->|verdict=fail<br/>fail_count<4| F
    H -->|verdict=pass| I[Learner agent<br/>direct LiteLLM]
    H -->|verdict=fail<br/>fail_count>=4| K[blocked]
    I --> J[git commit + push + gh pr create]
    J --> Z[done<br/>PR open]:::ok
    K --> Z2[blocked]:::fail
    classDef ext fill:#fffbe6,stroke:#aa8800
    classDef ok fill:#dcfce7,stroke:#16a34a
    classDef fail fill:#fee2e2,stroke:#dc2626
```

## Detailed sequence (one ticket, happy path)

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant API as NUC API :8799
    participant PG as Postgres :5432
    participant RUN as adk-runner (NUC)
    participant N4J as Neo4j :7687
    participant P as Planner (Qwen3.6-27B :1235)
    participant D as Doer (qwen-coder-next :1234)
    participant FB as Feedback (LiteLLM)
    participant L as Learner (LiteLLM)
    participant GH as GitHub

    U->>API: POST /api/tickets {title, body}
    API->>PG: INSERT row, status=todo
    API-->>U: 201 {identifier: ONE-N}

    Note over RUN: systemd Restart=always<br/>polls every ~12s

    RUN->>PG: claim_next_any() FOR UPDATE SKIP LOCKED
    PG-->>RUN: ONE-N row, status->in_progress
    RUN->>N4J: CREATE (:Session)-[:OF_TICKET]->(:Ticket)

    Note over RUN,P: ADK SequentialAgent runs sub_agents in order

    RUN->>N4J: search L2 facts (vector + fulltext)
    RUN->>N4J: search L3 SOPs by ticket labels
    RUN->>P: prompt = system + facts + ticket
    P-->>RUN: plan.md (## Goal / ## Files / ## Steps [ ] / ## Acceptance)
    RUN->>RUN: write <worktree>/.aiforge/plan.md
    RUN->>N4J: mirror :Turn for planner

    Note over RUN,D: LoopAgent (max 4 iter)

    loop until verdict=pass or fail_count >= 4
        RUN->>RUN: git worktree add (if first iter)
        RUN->>N4J: top-8 :Fact ABOUT subticket files (hybrid)
        RUN->>N4J: Aider RepoMap digest from L5 :File/:Symbol
        RUN->>D: prompt = doer system + facts + RepoMap + plan.md
        Note over D: enter_plan_mode(plan.md)<br/>GA tracks [ ]/[x] in file
        D->>D: file_read, file_patch, code_run mvn
        D->>RUN: chunks streamed, :Turn mirrored each turn
        D-->>RUN: final_answer + counters {edits, compile_green}
        RUN->>FB: prompt = ticket + diff + counters
        FB-->>RUN: verdict
    end

    alt verdict=pass
        RUN->>L: prompt = ticket + plan + diff + verdict
        L-->>RUN: distilled fact text
        RUN->>N4J: CREATE (:Fact {source: 'aiforge_learner'})-[:ABOUT]->(:Ticket)
        RUN->>RUN: git commit -m "ONE-N: <title>"
        RUN->>GH: git push + gh pr create
        GH-->>RUN: PR url
        RUN->>PG: UPDATE status=done, completed_at
    else verdict=fail (escalated)
        RUN->>PG: UPDATE status=blocked
        Note over RUN: worktree cleaned, no PR
    end

    RUN->>N4J: UPDATE :Session SET ended_at, outcome
    RUN->>RUN: exit (systemd respawns, polls next ticket)
```

## Memory touch-points

```mermaid
flowchart LR
    subgraph N4J[NUC Neo4j]
        L0[L0 :MetaSop]
        L2[L2 :Fact<br/>vector+fulltext]
        L3[L3 :Sop]
        L4[L4 :Session+:Turn]
        L5[L5 :File+:Symbol]
    end
    subgraph Hot[Doer host]
        AID[Aider SQLite cache]
    end
    subgraph S[ADK Session]
        L1[L1 working state]
    end

    Planner -->|read top-8| L2
    Planner -->|read by labels| L3
    Doer -->|read top-8 scoped| L2
    Doer -->|RepoMap digest| AID
    AID -.refresh.-> L5
    Doer -->|graph_lookup tool| L5
    Doer -->|ask_explorer recall| L4
    Feedback -->|read top-3| L2
    Learner -->|read SOP| L0
    Learner -->|write :Fact| L2

    PlannerTurn[every Planner turn] --> L4
    DoerTurn[every Doer turn] --> L4
    FbTurn[every Feedback turn] --> L4
    LeTurn[every Learner turn] --> L4

    L1 -.auto-mirror.-> L4
```

## Optimization touch-points (where context shrinks)

```mermaid
flowchart TD
    A[ticket body 1KB] --> B[Planner prompt]
    B --> B1[+ top-8 L2 facts ~2KB]
    B --> B2[+ matching L3 SOPs ~1KB]
    B --> P[Planner LiteLLM call<br/>~4KB total]
    P --> PLAN[plan.md ~1KB<br/>checkbox-driven]

    PLAN --> D[Doer prompt]
    D --> D1[+ top-8 scoped facts ~2KB]
    D --> D2[+ Aider RepoMap digest ~1.5KB]
    D --> D3[+ allowed-files list]
    D --> GA[GA loop<br/>tool schema 2.7KB INJECTED ONCE<br/>auto_save_tokens compacts on subsequent turns]
    GA --> GA2[GA_LANG=en<br/>English protocol]
    GA --> GA3[update_working_checkpoint dropped<br/>~900 chars saved/turn]

    D --> ASK[ask_explorer for big-context exploration<br/>spawns child GA process<br/>doer's own context stays clean]

    style B1 fill:#dbeafe
    style B2 fill:#dbeafe
    style D1 fill:#dbeafe
    style D2 fill:#dbeafe
    style ASK fill:#fef3c7
    style GA fill:#dcfce7
    style GA2 fill:#dcfce7
    style GA3 fill:#dcfce7
```

## Failure paths

```mermaid
stateDiagram-v2
    [*] --> todo: ticket created
    todo --> in_progress: claimed by adk-runner
    in_progress --> doer_running: planner emitted plan.md
    doer_running --> feedback_running: doer final_answer
    doer_running --> doer_running: compile_fail<2 retries
    feedback_running --> doer_running: verdict=fail, fail_count<4
    feedback_running --> learner_running: verdict=pass
    feedback_running --> blocked: verdict=fail, fail_count>=4
    feedback_running --> blocked: verdict=scope_violation
    doer_running --> blocked: max_turns or max_wall exceeded
    learner_running --> done: :Fact written, PR opened
    blocked --> [*]
    done --> [*]
```

## Live intervention (steer a running agent)

```mermaid
sequenceDiagram
    participant Op as Operator
    participant API as NUC API :8799
    participant TD as task_dir/_intervene
    participant Agent as Live Doer

    Op->>API: POST /api/tickets/ONE-N/intervene<br/>{kind:"intervene", body:"focus on src/main"}
    API->>TD: write _intervene file
    Note over Agent: turn_end_callback polls task_dir
    Agent->>TD: consume_file(_intervene)
    TD-->>Agent: hint string
    Agent->>Agent: prepend hint to next user prompt
    Note over Agent: continues with steered context
```

## File / dir touchpoints during one run

```
NUC
├── /home/mani/codeRepo/PosClientBackend
│   ├── .aiforge/plan.md                     <- planner writes
│   └── .aiforge-worktrees/ONE-N/             <- doer's git worktree
│       └── src/main/java/.../X.java          <- doer edits
├── /home/mani/genericagent/temp/aiforge-ONE-N-<ts>/
│   ├── _intervene                            <- intervention API writes
│   ├── _keyinfo                              <- intervention API writes
│   └── _stop                                 <- intervention API writes
├── /home/mani/.aiforge/logs/graph-runner.err  <- all agent traces
├── Postgres                                   <- ticket row + events
└── Neo4j                                      <- :Session, :Turn, :Fact
```

## What runs where (host map)

```mermaid
flowchart LR
    subgraph LAP[Laptop]
        OP[Operator<br/>SSH client only]
    end
    subgraph MS[Mac Studio 192.168.70.185]
        DOER[mlx_lm.server :1234<br/>Qwen3-Coder-Next]
        PLAN[mlx_lm.server :1235<br/>Qwen3.6-27B]
    end
    subgraph NUC[NUC 192.168.70.191 / 10.10.10.2]
        API[FastAPI :8799]
        RUNNER[adk-runner<br/>systemd Restart=always]
        PG[Postgres :5432]
        N4J[Neo4j :7687]
        GA[GenericAgent]
        AID[Aider lib]
        GFY[Graphify cron]
        REPOS[~/codeRepo/*]
    end

    OP --SSH--> NUC
    RUNNER --HTTP--> DOER
    RUNNER --HTTP--> PLAN
    RUNNER --asyncpg--> PG
    RUNNER --bolt--> N4J
    GFY -.nightly.-> N4J
```
