# OKR-DAG Memory

A Markdown-backed, goal-oriented memory: files are **nodes**, YAML frontmatter
carries **typed edges**, and an **in-memory DAG** (built at boot, no Neo4j)
drives *surgical* context retrieval → a compiled system prompt. Replaces the
flat topic-brief dump with an Objective → Key Result → Session/Learning graph.

## Decisions (locked)
- **A — add-DAG, demote Neo4j.** Neo4j stays optional; the OKR-DAG is the
  primary memory and needs no DB.
- **B — folder split.** Nodes live in typed folders under `~/.aiforge/memory/okr/`.
- **C — scope segregation.** The bundle splits into a `global/` subtree and one
  `projects/<repo>/` subtree per repo, so read never leaks one project's
  knowledge into another. A node's scope is DERIVED from its frontmatter
  (`workspace` / `scope: repo:<repo>`, else global). Ids stay globally unique
  per type (S-01, L-03) so cross-scope links keep resolving.

## Layout
```
~/.aiforge/memory/okr/
  global/                          universal knowledge (all repos)
    objectives/  O-<id>.md         the "why"  — long-lived goals
    key_results/ KR-<id>.md        the "what" — milestones (→ objective)
    learnings/   L-<id>.md         the "how"  — universal rules
    sessions/    <date>-<id>.md    the "when" — run logs
  projects/<repo>/                 knowledge specific to one repo
    repo/        R-<slug>.md       the CARD — build/test/run/structure/deploy/…
    learnings/   L-<id>.md         repo rules/conventions/gotchas
    solutions/   S-<id>.md         completed features/fixes (changelog)
    scripts/     SC-<id>.md        reusable shell/python scripts
    tasks/       T-<id>.md         small-task recipes ("how to do X here")
    sessions/    <date>-<id>.md
  index.md   (## Global / ## <repo> …, reserved, no frontmatter)
  log.md     (dated solution changelog, newest-first)
  .migrations.json  (one-shot migration marker)
```

## Node schemas (frontmatter)
| type | id | scope key | key fields |
|------|-----|-----------|-----------|
| objective  | `O-01`  | workspace? | status, priority, tags |
| key_result | `KR-01` | `parent_objective: O-01` | status, metrics |
| learning   | `L-01`  | `scope: global \| repo:<r> \| [O-01]` | category (topic) |
| session    | `2026-07-10-01` | workspace? | date, linked_krs |
| solution   | `S-01`  | workspace | kind (feature/fix), topic, tables, services, files, ticket |
| repo       | `R-<slug>` | workspace | stack, build, test, run, structure, entry_points, deploy, services, tables, gotchas, scripts, workflows |
| script     | `SC-01` | workspace | name, lang (shell/python), purpose, path, run |
| task       | `T-01`  | workspace | title, about, tags |

Body is markdown. The `repo` card is UPSERTED (one per repo — scalars overwrite,
lists union). solutions/scripts/tasks are deduped on write.

## Read (retrieval)
`retrieve.context_block(repo, query)` returns: the repo CARD first (the hub),
then its scripts, task recipes, and the learnings/solutions most RELEVANT to
`query` (exact → fuzzy → recency ranking), plus the global rules — and NOTHING
from other projects. `store.load_all()` caches the full parse on a dir
signature; writes are locked + reindex-deferrable for bulk.

## Migrations (auto, on boot)
`memory.migrations.run_startup_migrations()` upgrades any older memory into this
shape: legacy brief format → OKR envelope, `compacted-<topic>.md` briefs → OKR
learnings, old Neo4j Observation/Decision → md captures, flat `okr/` → scoped
`global/` + `projects/`, then an LLM `classify` sorts global learnings into their
project (or trash), and `build_repo_profiles` seeds each repo card. One-shot
steps are marker-guarded (`.migrations.json`).

## Pipeline (phases)
- **P1 — schema + store.** Typed nodes, folders, id allocation, save/load. ✅
- **P2 — in-memory DAG.** Parse all nodes → dict graph; edges from
  `parent_objective` / `linked_krs` / `linked_objectives` / `scope`. Rebuilt on
  change; an `active_context` pointer marks the current KR/Objective.
- **P3 — surgical retrieval + compile.** Ascend (Objective title = the *why*) +
  descend (active KR body = the *what*) + constraints (Learnings scope=global |
  active objective) + recent (last N sessions on the KR). Stitch into a bounded
  system prompt (`<OBJECTIVE>/<ACTIVE_TASK>/<CRITICAL_RULES>/<RECENT_ACTIVITY>`),
  wired into the context bundle.
- **P4 — auto-authoring.** The model extracts Objectives/KRs/Learnings from a
  session and writes them into the folders (the write side).

## No new heavy deps
stdlib + `yaml` (already used). DAG is plain dicts (no NetworkX); prompt compile
is a bounded template (no Jinja2 dep). `watchdog` is optional — a boot-time +
on-write rebuild covers the single-process case.

## Flat-brief scope memory (2026-07-12)

Alongside the OKR-DAG, the human-facing flat briefs (`compacted-<scope>.md` under
`~/.aiforge/memory/`, rendered through `runtime/work_notes.py`, the ONE Google-OKR
envelope) are now scope-aware end to end. **We stay on the OKR standard —
Objective + Key Results + Facts + Links + Learnings — and only tweak how it is
populated; no new sections.**

- **Scope classifier** — `md_store.classify_scope(text, hint_repo, hint_topic)`
  decides `global | project:<repo> | topic:<slug>` with the learner LLM
  (`AIFORGE_OKR_SCOPE_LLM`, default on; deterministic hint-honouring fallback when
  off/unreachable). `capture()` uses it to **promote** a repo-hinted but
  universally-true fact to the shared (global) brief.
- **OKR category mapping** — tickets worked = **Key Results** (the measurable
  work; the jira ref is also copied into **Links**); points-to-remember = **Facts**;
  lessons = **Learnings**. Enforced in the consolidation prompt.
- **Cross-scope mapping** — `md_store.map_scopes()` asks the LLM which briefs are
  related (project ↔ global ↔ topic) and writes **bidirectional** same-dir links
  (`[global](compacted-shared.md)`) into both briefs' Links. A `map_scopes` step
  runs in `force_recompact_all`.
- **Session-end compaction** — `runtime/chat_okr.compact_session(session_id, repo)`
  distils a transcript into atomic durable items (decisions, learnings,
  **meaningful user inputs only** — chit-chat dropped) and routes each to its
  scope via `capture`. Trigger `AIFORGE_SESSION_COMPACT` = `idle` (default) |
  `turns` | `explicit` | `off`; the idle daemon compacts sessions gone quiet for
  `AIFORGE_SESSION_IDLE_MIN` (default 30) min; explicit via
  `POST /api/chat/sessions/{id}/compact`.
- **Previous-session continuity** — at session start the chat agent injects
  `chat_okr.previous_session_brief()` (tail of the last session, framed as
  supersedable). `AIFORGE_SESSION_PREV_CONTEXT=0` disables.
- **Supersession** — when new info contradicts a stored fact, `AIFORGE_OKR_SUPERSEDE`
  = `archive` (default — drop the stale line, git keeps history) | `keep` (tag it
  `[superseded <date>]`, keep both).
- **Map→summarize recall** — `memory.recall_summary.summarize_hits(query, hits)`
  folds many scattered recall hits into ONE compact LLM briefing before injection
  (used by `memory_block.fetch` and chat `_memory_recall`).
  `AIFORGE_UMEM_SUMMARIZE` (default on), `AIFORGE_UMEM_SUMMARIZE_MIN` (default 5).
- **Self-heal** — `md_store.reheal_scopes()` re-classifies facts in project/topic
  briefs and moves globals to the shared brief. Heavy (LLM per fact) → opt-in via
  `AIFORGE_OKR_REHEAL=1`; runs as a `reheal` step in `force_recompact_all`.
