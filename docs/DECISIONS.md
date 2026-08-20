# Decisions Log (ADR-lite)

Why things are the way they are. One line per decision; every row verified
against the code or git history cited in Evidence. Newest-ish last within
each group.

## Agent core

| Decision | Why | Evidence | Date |
|---|---|---|---|
| Text ReAct protocol (`ACTION:`/`ARGS_JSON:`), not native tool-calling | Works on every OpenAI-compatible backend; local runtimes had broken `tool_calls` | `runtime/chat_agent.py` module docstring | 2026-06-20 |
| Two tool registries (chat text-protocol + Doer `FunctionTool`), never merged | Different calling conventions; drift is guarded by `tool_manifest.CROSS_SURFACE` + a CI parity test + a startup check | `runtime/tool_manifest.py`, `tests/python/runtime/test_chat_tool_parity.py` | 2026-06 |
| Inline-args parse rescue in `_parse` | Local models emit `ACTION: tool {...}` + empty `ARGS_JSON: {}`; the empty marker shadowed the good inline object → tool looped arg-less at temp 0. Rescue verified 6/6 on the NUC | `chat_agent.py` ~2650; commit `14cfbc0` | 2026-07-09 |
| dspy evaluated and REJECTED | 8 use-cases: parity or ours ahead in every cell; fixed-harness java/python/cpp codegen duel ours 75/75 vs dspy 58/61. Only lasting improvement (inline-args rescue) already shipped | commits `9f4f5cd`…`a990c03` | 2026-07-10 |
| Multi-ask checklist + `plan_progress` + FINAL completeness gate | #1 simple-mode complaint was answering part 1 of a multi-part ask and stopping; checklist is pinned high, model flips items live, and a one-time self-check gates FINAL | `chat_agent.py` ~3444, ~4029 | 2026-07 |
| Rules injected EVERY turn (mandatory) | User rules are constraints, not suggestions; always-on rules survive compaction and cave mode | `chat_agent.py` ~855/~988; `context_bundle.py` | 2026-06 |
| Workflows = mandatory procedures; injected before the repo map; survive cave mode; custom outranks builtin | A matched workflow is user procedure (branch/MR conventions etc.) — dropping it means skipping a mandatory step. Load order builtin → global → repo-local, later wins | `chat_agent.py` ~3745; `runtime/workflows.py` `load()` | 2026-06/07 |
| Workflow scripts: HARD run-before-save test gate | Each script in `<workflow>/scripts/` is syntax-checked AND its declared test command actually RUN; any failure refuses the whole save — no broken scripts persisted | `runtime/workflows.py` ~265/~343 | 2026-07-09 |
| Router doc-veto fix: a doc-word MENTION no longer vetoes a build | A blanket "mentions docs → not a build" veto mis-routed real multi-file builds; veto now applies only when NO strong code noun matched | `api/api.py` ~4016; commit `0530cd5` | 2026-07-09 |

## Pipeline

| Decision | Why | Evidence | Date |
|---|---|---|---|
| Parallel subtasks DEFAULT ON, max 4 workers | Operator decision; each subtask in its own git worktree; LM Studio slots / vLLM serve concurrently and win from 4. `AIFORGE_PARALLEL_SUBTASKS=0` opts out | `runtime/parallel_subtasks.py` docstring + `_max_workers()` | 2026-07-09 |
| SPEC.md written on EVERY pipeline-routed run, before any subtask | Isolated workers invent conflicting conventions without a shared contract; mid-run steering appends to it | `parallel_subtasks.py` ~1800; commit `9608265` | 2026-07-03 |
| Enhancer degenerate-output guard | The enhancer is a single point of failure — a collapsed rewrite (or one that lost every named file/symbol) poisons the whole run; the raw ask is restored | `pipeline.py: _make_enhancer_guard`; `parallel_subtasks.py: _spec_degenerate` | 2026-07 |
| Architect plan gate + exactly ONE semantic reask | Deterministic checks (file dump, code without tests, mixed languages); one model retry, then the sanitized plan ships anyway | `parallel_subtasks.py: _validate_plan` ~1447/~1520 | 2026-07 |
| Test-first: test subtasks build first; test backstop adds one per code module if the plan has none | "The test is the specification" — the build can be verified + self-healed | `parallel_subtasks.py` ~871, ~1920, `_ensure_test_coverage` | 2026-07 |
| Reconcile config-validity gate | Live-e2e finding: ONE unterminated string in a merged pyproject killed every pytest/pip run at config parse — the fix loop was blind. Config is parse-checked deterministically and fixed FIRST | `parallel_subtasks.py: _broken_project_config` ~3419; commit `ab835dc` | 2026-07-09 |
| Reconcile escalation: only the STUCK residual escalates to `AIFORGE_ESCALATION_MODEL` | A fresh reasoning model resolves what a looping coder can't; escalating every round would waste the big model | `parallel_subtasks.py` ~3445 | 2026-07 |
| Reconcile test-audit: a wrong test assertion may be corrected, visibly | Local models write buggy tests too; edits carry a `# test-audit:` marker; off via `AIFORGE_RECONCILE_TEST_AUDIT` | `parallel_subtasks.py` ~2839/~3460 | 2026-07 |
| Simple-mode multi-file BUILD auto-escalates into the pipeline; Plan mode never does | One agent can't hold a multi-file build coherent; plan is read-only by contract | `api/api.py` ~3995-4094 | 2026-07 |

## Memory & context

| Decision | Why | Evidence | Date |
|---|---|---|---|
| One context-assembly seam: `context_bundle.build_bundle()` | Chat and chat-team both route through it; no re-gathering elsewhere. Inject order: prefs → rules → project brief → skills → workflows → repo summary → repo map → memory | `runtime/context_bundle.py` | 2026-06 |
| `work/<kind>/<key>/` shared dossier folders | Work is ABOUT something durable (ticket/page/repo); one folder per context, shared across sessions, instead of scattering artifacts in throwaway session dirs | `runtime/work_context.py`; commit `bca63f6` | 2026-07-08 |
| `context_gather`: parallel cross-entity dossier, cached | Explaining a ticket needs its linked pages/tickets/images; fetched in parallel, merged to `dossier.md`, refreshed only when the entity changed | `runtime/context_gather.py`; commit `a4660d5` | 2026-07-08 |
| Own AST code chunker; chonkie kept for docs ONLY | chonkie's CodeChunker needs `tree-sitter-language-pack>=1.x`; aider-chat (0.86.2 locked) pins `==0.13.0` — unresolvable in one env. Line-window fallback so ingestion never breaks | `packages/aiforge_memory/pyproject.toml` comments; `features/chunk/chonkie_adapter.py` | 2026-07 |
| Memory recall/write mirrored to Langfuse | Make recall observable next to the LLM calls it feeds; fire-and-forget, soft-fails | `memory/unified_query.py` ~366 | 2026-07-09 |
| OKR-DAG memory: markdown nodes + in-memory graph, no DB | Goal-oriented recall (objective→KR→learning→session) beats a flat brief dump; typed frontmatter edges build a plain-dict graph at boot — no Neo4j/NetworkX. Surgical ascend/descend/constraints/recent → bounded prompt block | `memory/okf/`; `docs/OKR_MEMORY.md` | 2026-07-10 |
| Every agent writes through the OKR library (`capture`), tagged by agent+topic | One write path → topic-organized, deduped, split-on-oversize briefs; `agent:<role>` tag makes memory filterable by who wrote it | `runtime/tools/memory_write.py: _feed_brief`; `request_context.role` | 2026-07-10 |
| Session execution ledger + auto working-workflows (LLM-verified) | Stop the agent redoing completed steps; capture the commands that WORKED as a reusable workflow, verified before save | `runtime/session_ledger.py` | 2026-07-10 |
| Topic compaction hourly, split-on-oversize with cross-refs | Memory stays organized-by-topic within the hour; a topic that outgrows the cap splits into linked parts instead of trimming (losing content) | `memory/md_store.py`; `api._compact_chat_md` | 2026-07-10 |
| ALL local compaction folded into ONE daily evening pass (`AIFORGE_COMPACT_AT_HOUR`, default 18) | Every fold costs learner-LLM calls; hourly compaction + a per-idle-session fold re-folded briefs that barely moved, all day. One pass after the day's work: at-or-after the hour, once per local day, retried in an hour if it fails | `api._compact_at_hour` / `_daily_compact`; `runtime/periodic.py` | 2026-08-19 |
| Compaction NEVER runs before its hour — the missed-day catch-up waits for tonight | A laptop asleep at 18:00 was compacting at 09:00 the next morning, and the on-chat-switch fold + the boot-time `run_startup_migrations` fold ignored the hour entirely: three paths, one of which fires every time the lid opens. All three now share one window. Floor: `AIFORGE_COMPACT_MAX_SKIP_DAYS` (default 3) so a machine never awake in the evening cannot starve | `runtime/compact_window.py`; `periodic._Task.strict_hour`; `memory/migrations.run_startup_migrations` | 2026-08-20 |
| Scope classification batched — one LLM call per window of items, not per item | It was ~90% of a fold's traffic (90 calls / 172k prompt chars for one 72k-char chat day → 20 / 98k): every item re-sent the same 1.4k-char rule prompt | `md_store._scope.classify_scopes`; used by `chat_okr` + `_graph/_lint` | 2026-08-19 |
| Session folds are head-first and window-by-window; the offset advances only over turns the model SAW | Tail-truncating an 8k window and then advancing past every turn silently dropped the head — ~93% of a long single-chat day. An LLM failure no longer marks turns as folded either | `runtime/chat_okr.py: _transcript` / `_extract` | 2026-08-19 |
| Code chunks demoted in recall (score 0.4) | With a goal-oriented memory, raw RAG chunks shouldn't dominate; curated OKR/topic knowledge outranks them | `memory/unified_query.py: AIFORGE_UMEM_CHUNK_SCORE` | 2026-07-10 |

## Integrations & libraries

| Decision | Why | Evidence | Date |
|---|---|---|---|
| Adapter pattern for ALL third-party libs | `aiforge_core/integrations/` is the only import site; adapters raise, domain code owns fallback — a library swap touches one file | `integrations/__init__.py` docstring | 2026-07-09 |
| Langfuse mirror is SDK-free (raw REST) | The Python SDK's pins conflict with aider-chat; POST to `/api/public/ingestion` over httpx (already a core dep) needs zero extra installs | `integrations/langfuse_adapter.py` | 2026-07-09 |
| Self-hosted Langfuse v2 MINIMAL (app + postgres), not v3 | v3 adds clickhouse+redis+minio+worker (6 containers) for analytics we don't need; purpose is only to browse the agents' calls. Briefly upgraded to v3 then REVERTED — operator wanted no ClickHouse | `scripts/compose/langfuse-compose.yml` header; commit `43128d0` | 2026-07-10 |
| Mode is `AIFORGE_MODE`-driven; `--lite` = zero-Docker | A headless service picks lite (host + SQLite for tickets/chat/jobs/memory) via `.env` without editing its unit; the OKR-DAG memory is markdown so it needs no DB | `run.sh` MODE init; commit `dd6cb01` | 2026-07-10 |
| `--migrate`: Postgres → SQLite, then remove DB infra | Moving off Docker must carry the history — reads via the PG backends, writes via the SQLite backends in one process; keeps volumes (recoverable), aborts if migration fails. Tracing (langfuse) may stay as the only container | `scripts/migrate_to_sqlite.py`; `run.sh --migrate`; commit `bf4a3a4` | 2026-07-10 |
| Langfuse data deliberately ephemeral: 1-day retention, no volume | Operator requirement — no forever storage, no log accrual; hourly prune sidecar, logs capped 10MB×1 | `langfuse-compose.yml`; commit `0165126` | 2026-07-09 |
| Langfuse compose gets secrets via `--env-file`, not shell env | On hosts where docker needs sudo, `sudo docker compose` strips exported vars → postgres booted with an empty password and the stack aborted | `run.sh` langfuse section; commit `8880bec` | 2026-07-09 |
| ragas is a dev overlay, never an app dependency | Its langchain pins conflict with aider-chat in one resolution universe; run via `uv run --with 'ragas<0.4' …` | `integrations/ragas_adapter.py` | 2026-07 |
| Jira/Confluence config: env OR UI store, env wins at read time | Operator can override via `.env`/systemd without touching the UI; stored in `~/.aiforge/integrations.json` | `config/integrations.py` | 2026-06-24 |
| Jira/Confluence READS allowed in Plan mode + for pipeline roles | Reads are classified read-only (never prompt); Architect/Planner/Doer/Researcher get read allowlists in `agents.yaml`; the Plan-mode list drifted once and blocked them — kept in sync with `tool_policy` now | `chat_agent._READONLY_TOOLS` comments; `agents.yaml` | 2026-07 |

## Security rails

| Decision | Why | Evidence | Date |
|---|---|---|---|
| External writes default to the ASK approval gate | Jira/Confluence/GitLab writes, email_send, PRs/MRs, ipython, cron-job install all pause for Approve/Reject; reads never prompt | `tools/tool_policy.py: _DEFAULT_ASK` | 2026-06/07 |
| Blanket `git add -A/.` refused, targeted adds run | An agent must never stage unrelated junk; commit hygiene enforced in file tools AND shell | `chat_agent.py: _is_blanket_git` ~157 | 2026-06 |
| Destructive deletes need explicit confirmation | Agents act autonomously for everything EXCEPT deleting files/data; `AIFORGE_ALLOW_DELETE=1` opts out per process | `tools/delete_guard.py` | 2026-06 |
| `run_command` preflights missing paths | Catches a hallucinated path before the shell errors confusingly | `chat_agent.py: _preflight_missing_path` ~318 | 2026-06 |
| Web egress gated outside chat | `web_fetch`/`web_crawl` in headless roles need `AIFORGE_ALLOW_WEB_FETCH=1`; email is a config-gated integration, not arbitrary egress | `tools/web_search.py`, `web_ingest.py`, `email_tool.py` | 2026-06/07 |
| Per-role tool enforcement is layered | Tool schema filtered per role before the agent boots + call-time allowlist re-check; `finish` is Doer-only inside the tool itself | `agents/loader.tools_schema_for_role`; `tools/cognition.py` | 2026-05/06 |
| Doer edits clamped to the ticket's scope | ScopeGuard blocks mutations outside `scope_allowlist_globs` at the tool entry point | `runtime/scope_guard.py`, `tools/editor.py` | 2026-05/06 |
