# Chat projects and project memory — design

Date: 2026-10-01
Status: implemented on branch feat/chat-project-memory
Reference model: Cursor (open folder = project, project rules in the repo, user rules global, project wins on conflict).

## Goal

A user opens a new chat, picks a repo folder from the mounted path, and the chat
already knows that repo. Nobody has to explain the repo again. What one chat
learns about a project is available to every other chat on that project.
Global memory still applies everywhere. Memory must stay quiet: it must not
flood the prompt, interrupt the user, or fill up with junk.

## Scope

In scope: a new **Projects** page under the Operate group, and chats opened
from it. A project chat is the existing Chat screen bound to the project's
folder; its modes (simple, plan, team) are unchanged and get no new controls.

Out of scope: any project picker inside the Chat page or its mode selector,
tickets, jobs, a team-rules layer, a new project table, per-user memory.

## What exists today (verified on main e6f4cfee)

- `chat_sessions.cwd` is the only project binding. `POST /api/chat/sessions`
  accepts `cwd`, but no UI call site passes one, so every chat starts in a
  scratch folder `~/.aiforge/chat-workspaces/session-N`.
- Memory read and write are keyed by the repo name derived from `cwd`
  (`_chat_repo_key`). Two chats in the same repo already share memory.
- The always-on brief is ordered project, linked, global
  (`context_bundle.project_brief_text`).
- Recall unions project and global rows and ranks them together; scope does
  not affect rank. Cross-session chat recall is not filtered by project.
- A scratch chat writes memory under the key `session-<id>`, which no later
  chat ever reads.
- `<repo>/.aiforge/rules/*.md` and `<repo>/.aiforge/REPO_NOTES.md` are read
  live. `CLAUDE.md` is not in the live loader; the instruction ingester runs
  only from a CLI command.
- No API lists the folders under the mounted root. `repo_map.default_root()`
  reads `AIFORGE_WORKTREE_ROOT`, while the Docker mount sets
  `AIFORGE_REPO_ROOT`.
- The Memory page can list, read, create, compact, dedupe and delete memory
  files, but has no project filter and no per-project view.

## Design

### 1. Project = a picked folder

A project is a direct child folder of the mounted repos root. There is no new
table: the picked folder is stored in the existing `chat_sessions.cwd`, and the
project name is the folder name (the key memory already uses).

- New endpoint `GET /api/projects` returns the folders it looks in and the
  projects found, each with its chat count, last activity and memory numbers.
- Projects are looked for in the projects folder AND in every extra folder
  mounted into the box (Settings → Mounts, `--mount`). A mounted folder that
  is itself a repo is one project; one that holds repos lists its children.
- The Projects page is a list first, Cursor-style: one row per project you
  opened before (most recent first, removable without losing chats or
  memory), a "No repo" row for chats that belong to no project, and an "Add
  project" row that opens the folder picker. Clicking a row opens that
  project's chats in the usual chat layout, with a "‹ Projects" link back.
  The selection is in the URL (`/projects?path=…`).
- Opening a project is immediate: the chat shows at once and the project is
  registered and its memory synced in the background. The folder scan is
  cached for a few seconds and git is not called per folder.
- Any folder the box can see opens by typing its path: the Projects page has
  an "Open a folder" box with suggestions as you type
  (`GET /api/projects/browse`, `POST /api/projects/open`). In Docker only the
  projects folder and mounted folders can be opened; on a native install also
  folders under the user's home.
- The projects root is the configured repos base, else `AIFORGE_PROJECTS_ROOT`
  (now set by the `--repos` compose file), else the folder mounted when the
  API started, else the existing default. `AIFORGE_REPO_ROOT` itself is not
  read at request time, because ticket and team runs rebind it to one repo.
- Operate gets a new nav item, **Projects** (`/projects`), placed above Chat.
  The page lists the folders as cards: name, git or not, number of chats,
  memory size, last activity.
- Opening a card goes to `/projects/<name>`: the existing Chat screen with
  its session list filtered to that project and "New chat" creating a session
  with `cwd` set to the project folder. The header shows the project name and
  a link to its memory.
- `GET /api/chat/sessions` gains an optional `cwd` filter for that list.
- The Chat page (`/chat`) is not changed. A chat started there has no project,
  as today. A chat belongs to one project, or none, for its whole life.

### 2. Where memory lives: the `.aiforge` folder

Two folders, one per scope.

| Scope | Folder | Contents |
|---|---|---|
| Project | `<repo>/.aiforge/` | `rules/*.md`, `REPO_NOTES.md`, `memory/MEMORY.md` |
| Global | `~/.aiforge/` | `rules/`, the briefs, raw captures, `memory.db` |

- `<repo>/.aiforge/memory/MEMORY.md` is the project's compacted brief: plain
  markdown a person can read, edit and commit.
- It is a two-way mirror of the brief the memory store already keeps
  (`~/.aiforge/memory/compacted/compacted-<project>.md`). The store's file
  format, compaction and index are unchanged; the mirror is added beside them.
- Sync runs when a chat is opened on the project, when the brief is read for a
  first message, after every captured fact, and from the Memory page. An edited
  repo file is imported first, then the brief is written back out.
- When both sides changed since the last sync, the repo file wins and facts the
  store added meanwhile are kept. The last synced copy is stored so a fact the
  user deleted does not come back.
- A project that already has a brief keeps it; the first sync writes it into
  the repo. A repo that already has a `MEMORY.md` (pulled from git) is unioned
  with it.
- Raw captures and the search index (`memory.db`) stay in `~/.aiforge`. They
  never go into the repo.
- AIForge writes only inside `<repo>/.aiforge/`. It never edits `.gitignore`
  or any other file in the repo; whether to commit `.aiforge/` is the user's
  choice.
- If the repo is mounted read-only, or the write fails, the brief simply stays
  in `~/.aiforge` and the UI says "memory kept outside repo".
- Only the first part of a brief is mirrored. A brief that the existing topic
  compaction split into `-2`, `-3` parts keeps those parts in the store only.

### 3. Read: project first, global second

- The memory brief (project brief, then global brief, under one fixed budget
  spent in that order) is added to the system prompt on the **first message of
  a chat only**. Later messages do not carry it; they are told so and use the
  `memory_lookup` tool when they need a fact. `AIFORGE_CHAT_BRIEF=every`
  restores the per-turn behaviour.
- Repo rules (`.aiforge/rules`, `.cursor/rules`, `AGENTS.md`, `.cursorrules`)
  keep their existing per-turn behaviour. `CLAUDE.md` is not added there: it
  would repeat on every turn. It reaches the chat through project memory
  (section 7).
- Chat recall (opening turn, and the `memory_lookup` tool) searches the chat's
  own scope plus global as before, and additionally looks outside that scope.
- Own-project hits are lifted over equally relevant global hits
  (`AIFORGE_UMEM_OWN_PROJECT_BOOST`).
- A hit from outside the chat's scope is used only when it clears a relevance
  floor (`AIFORGE_UMEM_CROSS_MIN_SCORE`), at most `AIFORGE_UMEM_CROSS_MAX` per
  recall, at a lower weight (`AIFORGE_UMEM_WEIGHT_CROSS`), so an unrelated
  project cannot crowd out the chat's own. Each such hit is labelled with the
  project it came from. `AIFORGE_UMEM_CROSS_PROJECT=0` turns it off.
- Prior-chat recall puts sessions of the same project first.
- Ticket and pipeline recall are unchanged: they never look outside their repo.

### 4. Write: quiet by default

- A project chat writes learned facts to the project, using the existing
  gates (fact gate, subject folding, exact dedupe).
- A fact goes to global only through the existing promotion check (it names
  no file, path or symbol and is cross-project).
- A chat with no project files what it learns under one shared "general" key
  instead of a per-chat `session-<id>` key that nothing reads again.
- A chat writes only to its own scope. Reading another project's memory never
  writes to that project.
- No approval prompts. The existing "captured" pill stays the only signal,
  with its existing undo and rescope.
- Each chat has a "Learning on / off" switch. Off stops the learner, the
  session summary, the session fold and rule capture for that chat only.
  Reading memory is unaffected, and an explicit "remember this" still works.

### 5. Sharing across chats

Memory flows both ways; only the priority differs.

| Chat | Brief on first message | Recall, in priority order |
|---|---|---|
| On project A | A's brief, then global | A, global, then outside A (general + other projects) |
| No project | global | general, global, then every project |

- Every chat on project A reads what any other chat on A learned.
- A chat outside project A (another project, or no project) gets A's memory
  through recall when it is relevant to the question. It does not need the
  project to be named.
- A chat on project A gets memory from outside A the same way.
- Only the chat's own project and global are in the first-message brief. Memory
  from elsewhere arrives through recall alone, which keeps the prompt quiet.

### 6. Memory management

Memory that only grows becomes noise, so each project's memory is bounded and
visible.

- **Budget.** A project brief has a size cap (`AIFORGE_PROJECT_MEMORY_CAP`,
  chars). Past the cap, that project alone is folded into a shorter brief by
  the model role `AIFORGE_PROJECT_COMPACT_ROLE`. The previous version is
  archived under `~/.aiforge/memory/archive/projects/<project>/`; nothing is
  deleted. If no model answers, the brief is left exactly as it was.
- **Stale facts.** A fact that names files, none of which exist in the repo any
  more, is moved out of the brief into the project's stale list
  (`~/.aiforge/memory/projects/<project>.stale.json`). Stale facts are neither
  injected nor recalled. The user can restore or delete them. The check is
  conservative: a fact naming no file, a file outside the repo, a URL, or any
  file that still exists is left alone.
- **Daily upkeep.** One scheduled job runs the stale sweep for every opened
  project and compacts the ones over their cap. The existing daily dedupe is
  unchanged.
- **Memory page.** A "Project memory" panel: pick a project, see its size
  against the cap and when it was last compacted, edit and save the brief,
  Compact now, Move selection to global, Forget this project (archives the
  brief, removes its index rows and the repo's `MEMORY.md`), and the stale
  list with Restore / Delete.
- **In chat.** A project chat shows a link to that project's memory, and the
  Learning on / off switch.
- **Saving an edit** rewrites the brief, re-indexes it, and writes the repo
  file, so an edit takes effect on the next first message or lookup.
- Not built: a per-turn "memory used" chip.

API additions:

- `GET /api/projects`, `POST /api/projects/{name}/open`
- `GET /api/chat/sessions?cwd=`, `PATCH /api/chat/sessions/{id}/learn`
- `GET /api/memory/projects` — projects that have memory or were opened.
- `GET` / `PUT /api/memory/projects/{name}` — read and save the brief.
- `POST /api/memory/projects/{name}/compact`
- `POST /api/memory/projects/{name}/promote` — move lines to global.
- `POST /api/memory/projects/{name}/stale` — restore or delete a stale fact.
- `DELETE /api/memory/projects/{name}` — forget (archive, not delete).

### 7. First open: learn the repo once

When a chat is first opened on a project, a background job captures
`CLAUDE.md`, `AGENTS.md`, `GEMINI.md` and `.cursorrules` (top level of the
repo) into project memory. It records each file's hash and re-runs only for a
file whose hash changed. The chat is usable while it runs.
`AIFORGE_PROJECT_INGEST=0` turns it off.

## Errors

- Mounted root missing or empty: the Projects page shows "No folders found
  under <root>" with a link to the mounts setting; the Chat page still works.
- Project folder disappears later: `/projects/<name>` shows "not available";
  its chats stay listed on the Chat page and its memory is untouched.
- Repo not writable: fallback described in section 2.
- Two repos with the same folder name cannot occur, because projects are
  direct children of one root.
- Compaction fails: the brief is left as it was and the Memory page shows the
  error.

## Testing

Run on nuc, never on the Mac. Scope to changed files.

- Unit: folder listing and root resolution; scope ordering (own project,
  global and general, other projects); the relevance floor and per-turn cap
  for other-project hits; write routing for project, global and general;
  stale detection; cap triggers compaction; read-only fallback; ingest hash
  skip.
- API: the new repos and memory endpoints.
- End to end on nuc: open a project, teach it a fact in chat 1, open chat 2
  on the same project and confirm the fact is used without being asked; ask a
  related question in a chat with no project and in a chat on a second
  project and confirm the fact is recalled, labelled with its project; ask an
  unrelated question there and confirm it is not; teach a fact in a
  no-project chat and confirm a project chat recalls it; confirm
  `<repo>/.aiforge/memory/MEMORY.md` holds the project fact and no other repo
  file changed.
- Regression: team mode and tickets behave as before.

## Not building

Team rules, file-glob auto-attach for learned memory, a project table,
per-user memory, approval prompts, automatic `.gitignore` edits.


## Asking about a run while it goes on

A message typed while a run is in flight is one of four things, decided by the
server (`POST /api/chat/sessions/{id}/side`):

- **A status question** ("what's the status?", "how far along are you?", "are you
  stuck?"): answered at once from the run's own record, with no model call. It
  names the command running, the tool call the agent is in, the last few
  finished calls, files changed, how long it has been quiet, and any model
  wait. Nothing is queued and nothing is interrupted.
- **A correction** ("use the v2 endpoint instead", "also handle the empty
  list"): a steer. It is acknowledged at once with when it will be read, and a
  `command_wait` the agent is blocked in ends early so the agent reads it now.
  The command keeps running and the agent is told not to start it again.
  Stop or replace wording still ends the command.
- **An independent task** ("meanwhile summarise the README", a question about
  something else): a side agent, which is told the main run's live status. Its
  answer is shown in the main chat as soon as it is ready, and filed into the
  history once the main turn ends.
- Anything else while idle is an ordinary turn.
