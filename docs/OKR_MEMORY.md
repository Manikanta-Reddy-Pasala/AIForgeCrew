# OKR Memory

> **Current state (2026-07-13).** The memory is **flat scoped OKR briefs** —
> Markdown files under `~/.aiforge/memory/compacted/`, one per scope (global
> `shared` / per-repo / per-topic), each an OKR envelope (Objective / Key Results
> / Facts / Links / Learnings). The typed **node-DAG** described below is
> CONSOLIDATED OUT by default (`AIFORGE_OKR_DAG=0`; the `okr/` folder is archived
> to `memory-archive/` on boot) — kept for reference/reversibility, not active.
> See **"Flat-brief scope memory"** at the bottom + the summary below.
>
> **Layout**
> ```
> ~/.aiforge/memory/compacted/   the briefs (compacted-<scope>.md)
> ~/.aiforge/memory/archive/     folded raw captures (reversible)
> ~/.aiforge/memory/okf/         marker only (DAG consolidated out)
> ```
>
> **Setup / tools**
> - Embedder: **hash** (keyword + spell) by default; **model2vec** (vector KNN,
>   static embeddings, no torch) via `./run.sh --install-model2vec`, or **api**
>   (external `/v1/embeddings`). Env: `AIFORGE_EMBED_BACKEND=hash|model2vec|api`.
> - Seed memory from instruction files (committed, reproducible; stop api first):
>   `aiforge-memory-instructions --clear --root <repos-dir>`
>   (CLAUDE.md / AGENTS.md / GEMINI.md / .cursorrules; `--name` to add filenames).
> - Migrate a prior install into this layout: `./run.sh --migrate` (PG→SQLite,
>   Neo4j→briefs, root briefs→`compacted/`, okr DAG→archive).
>
> **Recall (hybrid, self-maintaining)** — `unified_query` fuses: semantic vector
> KNN (`memory`) + keyword/BM25 + spell (`keyword`) + **hot cache** of newest
> facts (`recent`) + **link expansion** (follows a hit brief's Links to sibling
> briefs). `/api/memory/search` splits results into **vector** vs **md** groups.
> A **seed TOC** of all briefs is injected into the chat prompt (so the model
> knows what to recall). Housekeeping: ONE evening pass (18:00 local, see below)
> — compact + full recompact — consolidate, **contradiction-resolve** (newer fact overwrites
> a stale contradicting one across repo/global), cross-scope link **map**, and a
> graph-health **lint** (dangling links / orphans).
>
> **Housekeeping runs ONCE A DAY, in the evening** (`AIFORGE_COMPACT_AT_HOUR`,
> default 18 local): one pass folds **every** session with new turns, then
> captures→briefs, then the full recompact. Every fold is LLM-heavy, so the old
> hourly + per-idle-session cadence spent tokens all day re-folding briefs that
> barely moved. Scheduling rules: fires the first time the daemon is awake
> **at or after** the hour; if a whole day passed with no run (a laptop that is
> never up at 18:00) it fires at the next wake whatever the hour; at most once
> per local day and never twice within 12h; the run — including a FAILED
> attempt and its retry count — is remembered in `~/.aiforge/periodic_state.json`,
> so a restart neither re-runs a finished pass nor buys a failing one extra
> attempts; a pass that fails retries after an hour, at most twice a day, and
> each of its three stages is isolated so one failure can't cancel the others.
> `AIFORGE_COMPACT_AT_HOUR=0` means OFF (as with the sibling `*_DAILY=0`
> knobs) — midnight is `24`. Session folds walk the backlog
> window by window (`AIFORGE_SESSION_COMPACT_CHARS` per window, at most
> `AIFORGE_SESSION_COMPACT_MAX_WINDOWS`, default 20, per session per pass); a
> session whose walk stopped short (window cap, model down, error) is revisited
> by the next pass even if no new message arrived, and a pass never overlaps
> itself. A retry skips the stages that already succeeded that day, so one
> broken session fold can't buy a second full recompact.
>
> **Scope classification is BATCHED** (`md_store.classify_scopes`): one call per
> window of items, not one per item. It was ~90% of a fold's traffic — a
> 72k-char chat day cost 90 LLM calls / 172k prompt chars, now 20 / 98k — because
> every item re-sent the classifier's rule prompt. `AIFORGE_SESSION_COMPACT_CHARS`
> is the ONLY transcript cap now; the extractor no longer re-truncates at 12k
> (which would have marked turns folded that the model never saw). The per-session
> marker carries `{offset, part, fails}` (a bare int still reads): `part` walks a
> single turn BIGGER than one window in slices instead of clipping it, and after
> `_MAX_WINDOW_FAILS` (3) failures on the same window it is skipped with a
> warning — a deterministic failure at temperature 0 repeats forever otherwise.
> A batched scope verdict the model never gave is marked `fallback: True`, and
> `cleanup_reheal` (which DELETES non-global facts) skips those.
> `AIFORGE_COMPACT_AT_HOUR=off` — or an explicit positive
> `AIFORGE_COMPACT_EVERY_H` — restores hourly compaction, the idle-session
> daemon (`AIFORGE_SESSION_IDLE_MIN`) and the nightly `AIFORGE_RECOMPACT_HOUR`
> recompact as three separate jobs.
>
> ⚠️ **`at_hour` never actually fired before 2026-08-19** — the due calculation
> always pointed at the *next* occurrence, which a sleep can only overshoot, so
> the documented "nightly 02:00 recompact" (and the Neo4j graph maintenance and
> the sqlite dedupe) had never run on a schedule. They do now: expect the daily
> pass to cost MORE per day than the old hourly compaction did, not less.
>
> Env knobs: `AIFORGE_SEED_TOC`, `AIFORGE_UMEM_RECENT`, `AIFORGE_UMEM_LINK_EXPAND`,
> `AIFORGE_OKR_CONTRADICT`, `AIFORGE_COMPACT_AT_HOUR` (default 18),
> `AIFORGE_RECOMPACT_HOUR` (default 2, only off the daily pass), `AIFORGE_OKR_DAG`.

---

*(Historical — the OKR-DAG design, now dormant behind `AIFORGE_OKR_DAG=1`.)*

A Markdown-backed, goal-oriented memory: files are **nodes**, YAML frontmatter
carries **typed edges**, and an **in-memory DAG** (built at boot, no Neo4j)
drives *surgical* context retrieval → a compiled system prompt. Replaces the
flat topic-brief dump with an Objective → Key Result → Session/Learning graph.

## Decisions (locked)
- **A — add-DAG, demote Neo4j.** Neo4j stays optional; the OKR-DAG is the
  primary memory and needs no DB.
- **B — folder split.** Nodes live in typed folders under `~/.aiforge/memory/okf/`.
- **C — scope segregation.** The bundle splits into a `global/` subtree and one
  `projects/<repo>/` subtree per repo, so read never leaks one project's
  knowledge into another. A node's scope is DERIVED from its frontmatter
  (`workspace` / `scope: repo:<repo>`, else global). Ids stay globally unique
  per type (S-01, L-03) so cross-scope links keep resolving.

## Layout
```
~/.aiforge/memory/okf/
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

It is also the ONE consumer of the peer-to-peer `view/` fold: a
`<SHARED_KNOWLEDGE>` block carries `okf.tiers.view_nodes()`, and the global rules
the fold already restates are dropped (`tiers.unrepresented`) so a fact held both
locally and in the view is rendered once. `mesh/` and `peers/` stay unread here —
they are inputs to the fold, not a second retrieval source
(`docs/superpowers/specs/2026-07-20-two-tier-knowledge-compaction.md`).

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
  `turns` | `explicit` | `off`; on the daily pass (`AIFORGE_COMPACT_AT_HOUR`)
  EVERY session with new turns is folded — `compact_session` is offset-based, so
  a session still in flight loses nothing, tomorrow's pass takes the rest. Off
  the daily pass the idle daemon compacts sessions gone quiet for
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
