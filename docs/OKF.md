# Open Knowledge Format (OKF v0.1)

AIForge's memory bundles follow **OKF v0.1** (Google Cloud) — a directory of
Markdown files that is a portable, linkable knowledge graph. Single source of
truth in code: [`aiforge_core/memory/okf.py`](../aiforge_core/memory/okf.py)
(`OKF_RULES` is injected into the compaction + learner LLM prompts so both
**produce** compliant files).

## 1. Hard rules (conformance)

- Every non-reserved `.md` file starts with a parseable **YAML frontmatter**
  block delimited by `---` on its own line.
- Frontmatter MUST contain a non-empty **`type:`** field. AIForge's OKR types:
  `objective`, `key_result`, `learning`, `session`, `solution`, `repo` (a
  repository's hub card), `script`, `task`.
- Every concept is a **UTF-8 Markdown** file: frontmatter + free-form body.
- **Scope layout** — the bundle splits into `global/<type>/` (universal) and
  `projects/<repo>/<type>/` (repo-specific); scope is derived from a node's
  `workspace`/`scope` frontmatter. See `docs/OKR_MEMORY.md`.

## 2. Identity & linking

- **Path is identity** — the file path *is* the concept id
  (`/projects/CacheLayer/learnings/L-03.md`, `/global/learnings/L-07.md`).
  No external IDs or databases.
- **Links** build the graph. Prefer **absolute** bundle-relative links
  (`/projects/CacheLayer/repo/R-cachelayer.md`); relative (`./other.md`) also allowed.
- **Untyped edges** — the *meaning* of a link (parent, depends-on, supersedes)
  lives in the surrounding prose, not the link syntax.

## 3. Reserved files

- **`index.md`** — navigation only; **no frontmatter**; lists contents.
  (AIForge regenerates it on every OKR save — see `okr/store._write_index`.)
- **`log.md`** — audit trail; flat list of **ISO-8601** date headings
  (`## 2026-07-11`), **newest first**.

## 4. Consumer mandates (be forgiving — never reject)

- Tolerate unknown `type` values and any unknown/custom frontmatter keys
  (preserve them).
- Tolerate broken cross-links (knowledge not yet written ≠ invalid).
- Never reject for missing optional fields, or a missing `index.md`/`log.md`.

## 5. Recommended optional fields (priority order)

1. `title` — human-readable name.
2. `description` — one-sentence summary (search snippet).
3. `resource` — URI of an underlying asset (DB table, repo path).
4. `tags` — YAML list of short strings.
5. `timestamp` — ISO-8601 datetime of last meaningful change.

## Where it is wired

| Producer | Hook |
|---|---|
| Compaction (fold notes → knowledge) | `work_notes._CONSOLIDATE_SYS` appends `OKF_RULES` |
| Learner (fact distillation) | `prompts/learner.py` — OKF concept/link note |
| OKR node renderer | `okr/nodes.render_node` (type + preserved recommended fields) |
| `index.md` | `okr/store._write_index` (regenerated per save) |
| Helpers | `okf.okf_frontmatter`, `okf.append_log`, `okf.render_index`, `okf.validate_file` |
