# Agent Role Redesign — Sr Dev / Developer / Tester pipeline (2026-04-20)

## Why change

Current approach fails the ONE-50 stock-transfer test:
- Gemma v1: 13 calls, no tests, no PR
- Qwen v1: 188 calls over 60 min, 5/13 tests, no commit
- Qwen v2: 110 tool calls over 24 min, zero new commits
- Only SMALLER single-bug tickets (ONE-50a, ONE-50b) converged (8 min, 2.4 min).

Root causes:
1. Tasks given as a lump — "fix 3 bugs + write 3 tests + run mvn + open PR" — agents drown in the task list.
2. One model tries to do analysis + coding + testing. Every model has a spiky capability profile.
3. No explicit retrieval/breakdown phase. Agents jump into coding, over-read source, run out of context.
4. No handoff protocol between phases — same agent keeps everything.

## New pipeline

```
Ticket → Sr Dev (thinking) → Developer (coder) → Tester (SWE-bench thinker) → Done
```

### Sr Dev — "The Thinking Model" (replaces EM in active loop)

| | |
|---|---|
| **Model** | `gemma-4-31b-it` (dense 31B, 18GB, ~64K ctx) |
| **Why** | Best single-shot reasoning in 4-bit MLX class. Wins BOI parser semantics (ONE-48). |
| **Input** | A high-level ticket. Possibly spans 1–N repos. |
| **Required layer use** | `hindsight_recall` (3×) → `rag` CLI (≤5 queries) → read 1–3 anchor files. |
| **Output** | A sub-task plan: numbered breakdown, per-repo branch names (`aiforge/TASK-ID`), test-case spec in plain English per sub-task, acceptance checklist. Written as a comment on the Paperclip ticket. |
| **Branching** | `git checkout -b aiforge/TASK-ID` in every involved repo (same branch name across repos). Sr Dev creates, nothing else. |
| **Non-goals** | Does NOT write implementation code. Does NOT write test code. Only prose + skeleton + spec. |

### Developer — "The Coder"

| | |
|---|---|
| **Model** | `qwen3-coder-next` (MoE 80B / 3B active, 42GB, ~65K ctx) |
| **Why** | SWE-trained on code diffs; highest breadth file-read → code-change throughput. |
| **Input** | ONE sub-task at a time from Sr Dev's breakdown. Context already filtered. |
| **Required layer use** | `rag` CLI (≤2 queries for idiom lookup). No need to re-analyze; Sr Dev did that. |
| **Output** | Code commit(s) on `aiforge/TASK-ID` branch. One commit per sub-task. No test authoring — Tester handles. |
| **Non-goals** | Does NOT expand scope. Does NOT refactor adjacent code. Does NOT plan. If spec is unclear, bounces back to Sr Dev. |

### Tester — "The Adversarial Thinker"

| | |
|---|---|
| **Model** | `Devstral-Small-2-24B-Instruct-2512` (dense 24B, 14GB, already on disk, SWE-agent-trained by Mistral). Swap to `qwen3.5-9b-mlx` if Devstral flaky. |
| **Why** | 24B dense fits the "lower-tier thinking + SWE-bench" criterion. Devstral is trained specifically on SWE-agent trajectories (code test/fix). |
| **Input** | Developer's branch + Sr Dev's test-spec comment. |
| **Required layer use** | `rag` CLI (find similar existing tests). |
| **Output** | Test commits on the SAME branch. Runs tests locally (or reports that JDK/Python not installed — accept compile-only check). Marks ticket `ready_for_review` if tests pass. Reopens sub-task if tests fail. |
| **Non-goals** | Does NOT fix implementation code (bounces back to Developer). Does NOT rewrite Sr Dev's spec. |

## Memory allocation on Mac Studio (64 GB unified)

| Model | GB | Active |
|-------|----|--------|
| gemma-4-31b-it (Sr Dev) | 18 | when Sr Dev is working |
| qwen3-coder-next (Developer) | 42 | when Developer is working |
| devstral-small-2-24b (Tester) | 14 | when Tester is working |

Cannot keep all three loaded. Orchestrator swaps per active role. Transition = `lms unload --all; lms load <model> -c <ctx>`. ~10 seconds per swap.

## Branch + handoff convention

1. Sr Dev creates `aiforge/TASK-ID` in every involved repo, pushes skeleton commit (or just the breakdown comment, no code).
2. Sr Dev comments ticket with:
   ```
   BREAKDOWN:
   - [ ] 50a.1: <subtask> (repo: MongoDbService)
   - [ ] 50a.2: <subtask> (repo: PosServerBackend)
   TEST SPEC:
   - Test X asserts Y
   - Test Z asserts W
   BRANCH: aiforge/TASK-ID
   READY_FOR_DEV
   ```
3. Paperclip trigger swaps assignee → Developer, swaps LM Studio model → qwen-coder.
4. Developer pulls, implements sub-tasks sequentially, commits one-per-subtask, pushes.
5. Developer comments: `READY_FOR_TEST` + list of sub-tasks marked done.
6. Paperclip trigger swaps assignee → Tester, swaps LM Studio model → devstral.
7. Tester writes tests matching Sr Dev's spec, commits, runs locally if possible.
8. Tester comments: `READY_FOR_REVIEW` + test results + `gh pr create` the branch.

## Comparison to current setup

| Dimension | Current | Proposed |
|-----------|---------|----------|
| Who analyzes? | Engineer agent itself | Sr Dev (dedicated thinking role) |
| Task granularity | Whole ticket to one agent | Broken into sub-tasks, each ≤15 min |
| Who writes tests? | Same agent as code | Separate Tester |
| Branch strategy | Per-model suffix (-reasoning / -coder) | Per-task `aiforge/TASK-ID` |
| Model swap discipline | Ad-hoc (script per ticket) | Role-based, Paperclip-triggered |
| EM role | Active (claude-opus for PM tasks) | Paused — only humans create tickets |
| Context per agent | Entire ticket | Just this role's narrow slice |

## Implementation plan

### Phase 1 — Role reconfiguration (1 hour)

1. Rename agent 28b8c064 `Sr Dev (Reasoning)` → `Sr Dev (Thinker)`. Title = "Senior Developer — Analysis & Planning". Stays on gemma-4-31b-it.
2. Rename agent e0502e94 `Sr Dev (Coder)` → `Developer`. Title = "Developer — Code Implementation". Stays on qwen3-coder-next.
3. Reactivate eb1c388d `Tester` → swap model to `devstral-small-2-24b` (was qwen3.5-9b-mlx — too small). Title = "Tester — Adversarial Verifier".
4. Pause 35760e2f `Engineering Manager` (already paused — keep that way).
5. Pause 0e173374 `Sr Architect` (already paused — keep).

### Phase 2 — AGENTS.md per role (1 hour)

Rewrite each agent's `~/.paperclip/instances/default/companies/fd294bd0-.../agents/<id>/instructions/AGENTS.md` with role-specific instructions:

- **Sr Dev (Thinker) AGENTS.md**: mandated hindsight+rag+read sequence, TodoWrite for breakdown, test-spec prose format, NO coding.
- **Developer AGENTS.md**: pick ONE sub-task from latest Sr Dev comment, implement, commit, bounce if unclear.
- **Tester AGENTS.md**: read Sr Dev spec + Developer branch, author tests, run if possible, comment results.

### Phase 3 — Lifecycle labels + routing (30 min)

Paperclip labels:
- `ready_for_analysis` → Sr Dev
- `ready_for_dev` → Developer
- `ready_for_test` → Tester
- `ready_for_review` → human

Hook or manual routing: when a comment from agent includes `READY_FOR_X` marker, Paperclip auto-reassigns + triggers model swap.

### Phase 4 — Model swap orchestration (2 hours)

Enhance `scripts/route-ticket.sh` to:
1. Detect new ticket assignee
2. Look up required model from agent's `adapter_config.model`
3. Unload current + load needed model in LM Studio
4. Wait for `/v1/models` to include target
5. Trigger Paperclip agent wake

Or: lightweight daemon `scripts/agent-orchestrator.sh` polls Paperclip agent_runtime_state every 30s and does the swap.

### Phase 5 — Validation run (2 hours)

Run the pipeline on a FRESH test ticket (not ONE-50 which is polluted). Propose:
- ONE-51: Add a new bank OCR parser (CENTRAL Bank) to PosPythonBackend
- Expected flow: Sr Dev breaks down → Developer codes handler → Tester writes tests.
- Success = single `aiforge/ONE-51` branch, 1 commit per agent, green tests, PR opened.

### Phase 6 — Review + tune (ongoing)

- Track per-role cycle time
- Track breakdown quality (did Sr Dev's sub-tasks match what Developer actually did?)
- Adjust model assignments if Tester's devstral underperforms (fallback: qwen3.5-9b or cloud kimi-k2.5)

## Open questions

1. **Devstral on-disk verification**: Already showed up in `lms ls` earlier as "Devstral-Small-2-24B-Instruct-2512". Need smoke-test it can do Hermes tool-calling (some models drop structured function calls).
2. **Branch conflict**: What if 2 tickets overlap on same file? Pipeline assumes one ticket at a time. Queue is serial, not parallel. Acceptable for single-user Mac Studio.
3. **Tester-to-Developer bounce**: How many rounds before the ticket is marked "needs human review"? Suggest: max 2 bounces.
4. **Sr Dev too thin?**: If a ticket is truly trivial (typo fix), Sr Dev still has to break down. Allow bypass: if ticket has label `direct_dev`, skip Sr Dev.

## Decision needed

Proceed with Phase 1 now? If yes, I renam+ reactivate agents, update AGENTS.md files, and swap Tester model. Then we dispatch ONE-51 as validation.

Or tweak the plan first?
