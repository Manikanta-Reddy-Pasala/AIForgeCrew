# OKR-DAG Memory

A Markdown-backed, goal-oriented memory: files are **nodes**, YAML frontmatter
carries **typed edges**, and an **in-memory DAG** (built at boot, no Neo4j)
drives *surgical* context retrieval → a compiled system prompt. Replaces the
flat topic-brief dump with an Objective → Key Result → Session/Learning graph.

## Decisions (locked)
- **A — add-DAG, demote Neo4j.** Neo4j stays optional for vector recall; the
  OKR-DAG is the primary goal-oriented memory and needs no DB.
- **B — folder split.** Nodes live in typed folders under `~/.aiforge/memory/okr/`.

## Layout
```
~/.aiforge/memory/okr/
  objectives/   O-<id>.md    the "why"  — long-lived goals
  key_results/  KR-<id>.md   the "what" — measurable milestones (→ objective)
  sessions/     <date>-<id>.md the "when" — commands/outputs (→ KRs)
  learnings/    L-<id>.md     the "how"  — rules/constraints (global | → objectives)
```

## Node schemas (frontmatter)
| type | id | edges | key fields |
|------|-----|-------|-----------|
| objective  | `O-01`  | — | status, priority, tags, created_at |
| key_result | `KR-01` | `parent_objective: O-01` | status, metrics |
| learning   | `L-01`  | `scope: global \| [O-01]` | category |
| session    | `2026-07-10-01` | `linked_krs: [KR-01]` | date |

Body is markdown (context / requirements / rule / logs).

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
