# OKR Memory

The memory is **flat scoped OKR briefs**: Markdown files under
`~/.aiforge/memory/compacted/`, one per scope (global `shared`, per-repo,
per-topic), each an OKR envelope — Objective / Key Results / Facts / Links /
Learnings. No database. The on-disk file format is [OKF.md](OKF.md).

The typed **node-DAG** (objectives → key results → learnings → sessions) is
consolidated out by default (`AIFORGE_OKR_DAG=0`; the `okf/` folder is archived
on boot). It is kept for reference and reversibility — see
[the dormant design](#dormant-the-okr-dag) at the bottom.

## Layout

```
~/.aiforge/memory/compacted/   the briefs (compacted-<scope>.md)
~/.aiforge/memory/archive/     folded raw captures (reversible)
~/.aiforge/memory/okf/         marker only (DAG consolidated out)
```

## Setup

| Thing | How |
|---|---|
| Embedder | **hash** (keyword + spell) by default; **model2vec** (vector KNN, static embeddings, no torch) via `./run.sh --install-model2vec`; **api** (external `/v1/embeddings`). `AIFORGE_EMBED_BACKEND=hash\|model2vec\|api` |
| Seed from instruction files | `aiforge-memory-instructions --clear --root <repos-dir>` — CLAUDE.md / AGENTS.md / GEMINI.md / .cursorrules (`--name` adds filenames). Stop the api first |
| Upgrade a prior install | `./run.sh --migrate` forces a re-converge |
| Maintenance, then exit | `./run.sh --dedupe` · `--recompact-all` · `--migrate-okf` · `--purge-code` |

## Recall

`unified_query` fuses four sources: semantic vector KNN (`memory`),
keyword/BM25 + spell (`keyword`), a **hot cache** of the newest facts
(`recent`), and **link expansion** (follows a hit brief's Links to sibling
briefs). `/api/memory/search` splits results into **vector** vs **md** groups. A
**seed TOC** of all briefs is injected into the chat prompt so the model knows
what is there to recall. Code chunks are demoted to score 0.4
(`AIFORGE_UMEM_CHUNK_SCORE`) so curated knowledge outranks raw RAG.

Many scattered hits are folded into ONE compact briefing before injection
(`memory/recall_summary.summarize_hits`; `AIFORGE_UMEM_SUMMARIZE`, on by
default, above `AIFORGE_UMEM_SUMMARIZE_MIN` hits).

## Housekeeping: one evening pass

**Everything folds once a day, in the evening** (`AIFORGE_COMPACT_AT_HOUR`,
default 18 local). One pass folds every session with new turns, then
captures→briefs, then a full recompact — consolidate, **contradiction-resolve**
(a newer fact overwrites a stale contradicting one across repo/global),
cross-scope link **map**, and a graph-health **lint** (dangling links, orphans).

Every fold is LLM-heavy, which is the whole point of the schedule: the old
hourly + per-idle-session cadence spent tokens all day re-folding briefs that
had barely moved.

| Rule | Detail |
|---|---|
| Never early | Fires the first time the daemon is awake **at or after** the hour, never before. A laptop asleep at 18:00 waits for tonight, instead of compacting at 09:00 the next morning |
| Never starves | After `AIFORGE_COMPACT_MAX_SKIP_DAYS` (3) days with no run it catches up whatever the hour |
| Never twice | At most once per local day, never twice within 12h |
| Survives restart | The run — including a FAILED attempt and its retry count — is remembered in `~/.aiforge/periodic_state.json`, so a restart neither re-runs a finished pass nor buys a failing one extra attempts |
| Retries | A failed pass retries after an hour, at most twice a day; the three stages are isolated so one failure cannot cancel the others, and a retry skips stages that already succeeded |
| `=0` means OFF | Midnight is `24` — but `24` normalises to hour 0, which is "due all day". Pick a real evening hour if you want the window to bite |

**The same window gates the two off-schedule folds**: the LLM fold fired when
you open a new chat (`chat_session_fold.fold_async`) and the boot-time
compaction in `run_startup_migrations`, which used to run one learner call per
brief on every API start — i.e. every morning the lid opens. Outside the window
the startup pass still folds structurally, just without the model
(`AIFORGE_STARTUP_COMPACT=always|window|off`). **Deleting a session folds
immediately at any hour**: those turns are about to be destroyed.

`AIFORGE_COMPACT_CATCH_UP=1` restores the old run-at-the-next-wake behaviour.
`AIFORGE_COMPACT_AT_HOUR=off`, or an explicit positive `AIFORGE_COMPACT_EVERY_H`,
restores hourly compaction, the idle-session daemon (`AIFORGE_SESSION_IDLE_MIN`)
and the nightly `AIFORGE_RECOMPACT_HOUR` recompact as three separate jobs.

> **`at_hour` never actually fired before 2026-08-19.** The due calculation
> always pointed at the *next* occurrence, which a sleep can only overshoot, so
> the documented "nightly 02:00 recompact" (and the graph maintenance, and the
> sqlite dedupe) had never run on a schedule. They do now — expect the daily
> pass to cost MORE per day than the old hourly compaction did, not less.

### Session folds are head-first

Folds walk the backlog **window by window**
(`AIFORGE_SESSION_COMPACT_CHARS` per window, at most
`AIFORGE_SESSION_COMPACT_MAX_WINDOWS`, default 20, per session per pass), and
the offset advances only over turns the model actually SAW. Tail-truncating a
window and then advancing past every turn silently dropped the head — about 93%
of a long single-chat day.

- The per-session marker carries `{offset, part, fails}` (a bare int still
  reads). `part` walks a single turn bigger than one window in slices instead of
  clipping it; after 3 failures on the same window it is skipped with a warning,
  because a deterministic failure at temperature 0 repeats forever.
- A session whose walk stopped short (window cap, model down, error) is
  revisited by the next pass even if no new message arrived. A pass never
  overlaps itself.
- A pass that ends with turns still pending reports FAILURE, so the scheduler's
  retry actually engages.
- `AIFORGE_SESSION_COMPACT` selects only the DAEMON trigger — with the daily
  pass on, anything but `off` folds.

### Scope classification is batched

One LLM call per window of items, not one per item
(`md_store.classify_scopes`). It had been ~90% of a fold's traffic: a 72k-char
chat day cost 90 calls / 172k prompt chars, now 20 / 98k, because every item
re-sent the same 1.4k-char rule prompt. `AIFORGE_SESSION_COMPACT_CHARS` is the
only transcript cap now. A batched verdict the model never gave is marked
`fallback: True`, and `cleanup_reheal` (which DELETES non-global facts) skips
those.

## Scope, end to end

| Piece | What it does |
|---|---|
| **Classifier** | `md_store.classify_scope(text, hint_repo, hint_topic)` → `global \| project:<repo> \| topic:<slug>` via the learner LLM (`AIFORGE_OKR_SCOPE_LLM`, on; deterministic hint-honouring fallback when off). `capture()` uses it to **promote** a repo-hinted but universally-true fact to the shared brief |
| **OKR mapping** | tickets worked = **Key Results** (the jira ref is also copied into Links); points-to-remember = **Facts**; lessons = **Learnings**. Enforced in the consolidation prompt |
| **Cross-scope map** | `md_store.map_scopes()` asks which briefs are related and writes **bidirectional** links into both briefs' Links |
| **Session-end fold** | `chat_okr.compact_session(session_id, repo)` distils a transcript into atomic durable items — decisions, learnings, meaningful user inputs only, chit-chat dropped — and routes each to its scope. Offset-based, so a session still in flight loses nothing |
| **Previous-session continuity** | at session start the agent injects `chat_okr.previous_session_brief()`, framed as supersedable (`AIFORGE_SESSION_PREV_CONTEXT=0` disables) |
| **Supersession** | `AIFORGE_OKR_SUPERSEDE=archive` (default — drop the stale line, git keeps history) or `keep` (tag it `[superseded <date>]`) |
| **Self-heal** | `md_store.reheal_scopes()` re-classifies facts and moves globals to the shared brief. Heavy (one LLM call per fact) → opt-in via `AIFORGE_OKR_REHEAL=1` |

Other knobs: `AIFORGE_SEED_TOC`, `AIFORGE_UMEM_RECENT`,
`AIFORGE_UMEM_LINK_EXPAND`, `AIFORGE_OKR_CONTRADICT`.

---

## Dormant: the OKR-DAG

*(Behind `AIFORGE_OKR_DAG=1`. Not active — kept for reversibility.)*

Files are **nodes**, YAML frontmatter carries **typed edges**, and an in-memory
DAG (built at boot, plain dicts — no Neo4j, no NetworkX) drives surgical
retrieval into a compiled prompt block.

**Decisions (locked).** Neo4j stays optional and the DAG needs no DB. Nodes live
in typed folders under `~/.aiforge/memory/okf/`. The bundle splits into a
`global/` subtree and one `projects/<repo>/` subtree per repo, so a read never
leaks one project's knowledge into another; scope is DERIVED from frontmatter
(`workspace` / `scope: repo:<repo>`, else global), and ids stay globally unique
per type so cross-scope links keep resolving.

```
~/.aiforge/memory/okf/
  global/                          universal knowledge (all repos)
    objectives/  O-<id>.md         the "why"  — long-lived goals
    key_results/ KR-<id>.md        the "what" — milestones (→ objective)
    learnings/   L-<id>.md         the "how"  — universal rules
    sessions/    <date>-<id>.md    the "when" — run logs
  projects/<repo>/
    repo/        R-<slug>.md       the CARD — build/test/run/structure/deploy
    learnings/   L-<id>.md         repo rules/conventions/gotchas
    solutions/   S-<id>.md         completed features/fixes (changelog)
    scripts/     SC-<id>.md        reusable shell/python scripts
    tasks/       T-<id>.md         small-task recipes
    sessions/    <date>-<id>.md
  index.md   (reserved, no frontmatter)   log.md   (dated, newest-first)
```

| type | id | scope key | key fields |
|---|---|---|---|
| objective | `O-01` | workspace? | status, priority, tags |
| key_result | `KR-01` | `parent_objective: O-01` | status, metrics |
| learning | `L-01` | `scope: global \| repo:<r> \| [O-01]` | category (topic) |
| session | `2026-07-10-01` | workspace? | date, linked_krs |
| solution | `S-01` | workspace | kind, topic, tables, services, files, ticket |
| repo | `R-<slug>` | workspace | stack, build, test, run, structure, entry_points, deploy, gotchas |
| script | `SC-01` | workspace | name, lang, purpose, path, run |
| task | `T-01` | workspace | title, about, tags |

The `repo` card is UPSERTED (one per repo — scalars overwrite, lists union);
solutions/scripts/tasks are deduped on write.

**Read.** `retrieve.context_block(repo, query)` returns the repo CARD first
(the hub), then its scripts, task recipes, and the learnings/solutions most
relevant to `query` (exact → fuzzy → recency), plus the global rules — and
nothing from other projects. It is also the one consumer of the fleet-sync
`view/` fold: a `<SHARED_KNOWLEDGE>` block carries `okf.tiers.view_nodes()`, and
global rules the fold already restates are dropped so a fact held both locally
and in the view renders once. `mesh/` and `peers/` stay unread here — they are
inputs to the fold, not a second retrieval source.

**Migrations.** `memory.migrations.run_startup_migrations()` upgrades older
memory into this shape — legacy briefs → OKR envelope, `compacted-<topic>.md` →
learnings, flat `okr/` → scoped `global/` + `projects/`, then an LLM `classify`
sorts global learnings into their project, and `build_repo_profiles` seeds each
repo card. One-shot steps are marker-guarded (`.migrations.json`).
