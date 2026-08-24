# Tool Reference

Complete reference for every agent tool. Sources of truth:
`aiforge_core/runtime/chat_agent.py` (`TOOLS` registry, 93 tools),
`aiforge_core/runtime/doer_tools.py` (pipeline `FunctionTool` registry),
`aiforge_core/agents/agents.yaml` (per-role allowlists),
`aiforge_core/runtime/tools/tool_policy.py` (gating).

Two registries, one contract: chat speaks the text ReAct protocol
(`handler(args, cwd) → dict`), the pipeline Doer uses typed ADK
`FunctionTool`s. Tools that must exist on both surfaces are declared in
`runtime/tool_manifest.py` (`CROSS_SURFACE`) — a CI parity test plus a
startup check fail loudly on drift.

## Gating legend

| Mark | Meaning | Enforced by |
|---|---|---|
| RO | Read-only — never prompts, allowed in Plan mode | `tool_policy._READONLY_ALWAYS_ALLOW` + `chat_agent._READONLY_TOOLS` |
| ASK | Pauses for Approve/Reject in chat | `tool_policy._DEFAULT_ASK` → `chat_approve` |
| RISK | Command text is risk-classified; dangerous → ASK, caution → ASK by default | `tools/command_risk.py` via `tool_policy` |
| EDIT | Write is held by the diff-preview review gate; deletes need confirmation | `chat_agent._MUTATING`, `tools/delete_guard.py` |

Plan mode blocks everything not RO (`chat_agent._READONLY_TOOLS`, ~line 2000).
`AIFORGE_TOOL_POLICY="tool=allow|ask|deny"` overrides any default.

## Chat agent tools (full registry — 93 + `plan_progress`)

### Files & editing

| Tool | Args | Does | Gate |
|---|---|---|---|
| `file_read` | `{path}` | read a file | RO |
| `read_lines` | `{path, start, end}` | read a line range | RO |
| `list_dir` | `{path}` | list a directory | RO |
| `file_write` / `file_create` | `{path, content}` | create/overwrite (syntax-checked; `force` to override) | EDIT |
| `file_patch` | `{path, old_text, new_text}` | targeted replace, syntax-checked | EDIT |
| `editor` | `{command: view\|create\|str_replace\|insert\|undo_edit, path, …}` | structured editor with undo; view is RO | EDIT (writes) |
| `multi_edit` | `{edits: [{path, old_str, new_str}…]}` | batch edits across files, validated then all-or-nothing | EDIT |
| `rename_symbol` | `{path, old, new}` | project-wide symbol rename | EDIT |

### Search & navigation

| Tool | Args | Does | Gate |
|---|---|---|---|
| `grep` | `{pattern, path}` | ripgrep recursive search | RO |
| `find` | `{name, kind, glob}` | fuzzy-locate files/dirs | RO |

(The AST repo map and graphify graph are injected as *context* in chat; the
`repo_map` / `graphify_lookup` *tools* are Doer/pipeline-side — see below.)

### Code execution & verification

| Tool | Args | Does | Gate |
|---|---|---|---|
| `run_command` | `{cmd, timeout}` | shell command (timeout in seconds); missing-path preflight; blanket `git add -A/.` refused | RISK |
| `run_tests` | `{mode, pattern}` | project test runner, per-test filter | allow |
| `typecheck` | `{}` | tsc/mypy/go vet … | allow |
| `format` | `{path}` | ruff/prettier/gofmt | EDIT |
| `lsp` | `{command, path, line, character}` | goto-def / find-refs / hover | RO |
| `project` | `{action: build\|test\|run}` | detect + install + build/test/run | RISK-adjacent (runs toolchain) |
| `ensure_runtime` | `{tools: [java, mvn…]}` | install + verify missing toolchain | allow |
| `serve` | `{cmd, port}` | background dev server (cmd is risk-assessed) | RISK |
| `stop_service` / `list_services` | `{pid}` / `{}` | stop / list `serve`d processes | allow / RO |
| `execute_ipython_cell` | `{code}` | persistent Jupyter kernel | ASK |
| `browse` | `{action, url…}` | Playwright browser automation | allow (degrades if absent) |
| `mcp` | `{server, tool, args}` | call an MCP server tool | allow |
| `delegate` / `delegate_to_agent` | `{agent, task}` | spawn a sub-agent (depth-capped) | allow |

### Git / VCS

| Tool | Args | Does | Gate |
|---|---|---|---|
| `git_status` / `git_diff` / `git_log` / `git_blame` | `{…}` | repo inspection | RO |
| `github_pr` | `{title, body, base, draft}` | open a GitHub PR via `gh` | ASK |
| `gitlab_mr_create` / `gitlab_mr_comment` | `{project, source_branch…}` / `{project, iid, body}` | open / comment a GitLab MR | ASK |

### Jira (21 tools)

| Tool | Args | Does | Gate |
|---|---|---|---|
| `jira_search` | `{query}` or `{jql, time}` | find issues (optionally with time fields) | RO |
| `jira_read` | `{key}` | issue + comments + time tracking | RO |
| `jira_worklog` | `{key}` | logged time: who/how much/when + rollup | RO |
| `jira_remote_links` | `{key}` | linked Confluence pages + web links | RO |
| `jira_myself` / `jira_projects` | `{}` | current user / visible projects | RO |
| `jira_boards` / `jira_sprints` / `jira_sprint_issues` | `{project}` / `{board_id, state}` / `{sprint_id, time}` | Agile boards → sprints → issues | RO |
| `jira_dashboards` / `jira_dashboard_read` | `{}` / `{id}` | list / read dashboards + gadgets | RO |
| `jira_transitions` | `{key}` | available workflow transitions | RO |
| `jira_resolve_project` | `{name}` | loose name → real project key | RO |
| `jira_create` | `{project, summary, issuetype, description}` | new issue | ASK |
| `jira_update` | `{key, …, status}` | edit fields; `status` auto-routes a transition | ASK |
| `jira_comment` | `{key, body}` | add a comment | ASK |
| `jira_transition` / `jira_assign` / `jira_link_issues` | `{key, …}` | move status / assign / link | ASK |
| `jira_log_work` | `{key, time_spent, comment}` | record time | ASK |
| `jira_dashboard_create` | `{name, description, share}` | create a dashboard (Cloud only) | ASK |

### Confluence (14 tools)

| Tool | Args | Does | Gate |
|---|---|---|---|
| `confluence_search` | `{query}` or `{cql}` | find pages | RO |
| `confluence_read` | `{id}` or `{title, space}` | read a page (storage XHTML) | RO |
| `confluence_spaces` / `confluence_page_by_title` | `{}` / `{space, title}` | list spaces / exact-title lookup | RO |
| `confluence_children` / `confluence_descendants` | `{id}` | direct children / all descendants | RO |
| `confluence_labels` / `confluence_comments` | `{id}` | read labels / comments | RO |
| `confluence_resolve_space` | `{name}` | loose name → real space key | RO |
| `confluence_create` / `confluence_update` | `{title, space, body…}` / `{id, body…}` | new / edit page | ASK |
| `confluence_add_label` / `confluence_comment` / `confluence_attach` | `{id, …}` | add label / comment / attachment | ASK |

### GitLab issues

| Tool | Args | Does | Gate |
|---|---|---|---|
| `gitlab_search` / `gitlab_read` | `{query, project, state}` / `{project, iid}` | find / read issues | RO |
| `gitlab_create` / `gitlab_update` / `gitlab_comment` | `{project, …}` | write ops | ASK |

### GitLab CI pipelines

| Tool | Args | Does | Gate |
|---|---|---|---|
| `gitlab_pipelines` | `{project, ref, status, sha, limit}` | list recent pipelines, newest first | RO |
| `gitlab_pipeline` | `{project, pipeline_id \| ref \| sha, logs, log_chars}` | one pipeline: status, jobs, log tail of what failed | RO |
| `gitlab_pipeline_watch` | `{project, …, interval_s, timeout_s, max_checks}` | poll until it finishes — ONE call covers the whole watch | RO |

`ok` says the read/watch worked; `passed` says the pipeline was green — they are
deliberately separate. A watch that runs out of budget returns `timed_out: true`
with the last status it saw rather than inventing an outcome; if it never
managed to read the pipeline at all it returns `ok: false` and no `passed` key,
because it observed nothing. `manual` is reported as finished-but-blocked, an
unknown status keeps the watch going, and `allow_failure` jobs are never blamed.
The watch budget is clamped to ~180s only where nothing can Stop it (the jobs
runner, `/api/chat/agent`); team mode and subtask runs re-bind the session so
Stop reaches them and the full `timeout_s` applies. When the clamp bites, the
effective value comes back as `unattended_budget_s`.

### Email & web

| Tool | Args | Does | Gate |
|---|---|---|---|
| `email_read` | `{query, limit, folder}` | read inbox via IMAP | RO |
| `email_send` | `{to, subject, body, cc, bcc}` | send via SMTP | ASK |
| `web_search` | `{query, limit}` | Tavily/Brave keyed, DuckDuckGo fallback | RO; egress-gated in headless roles |
| `web_fetch` | `{url, max_chars}` | read a page's text | RO; `AIFORGE_ALLOW_WEB_FETCH=1` egress gate outside chat |
| `web_crawl` | `{url}` | page → clean markdown, saved to `work/web/<slug>/` dossier | RO; same egress gate |

### Context, resolvers & settings

| Tool | Args | Does | Gate |
|---|---|---|---|
| `context_gather` | `{kind: jira\|confluence, key}` | parallel cross-entity dossier (entity + linked pages/tickets/images), cached in the work folder | RO |
| `resolve_repo` | `{name}` | loose repo/service name → local path | RO |
| `search_chat_sessions` | `{query, limit}` | recall past chat sessions | RO |
| `list_repos` | `{}` | configured base folder + per-repo paths | RO |
| `set_repo_folder` / `set_repo_root` | `{repo, path}` / `{path}` | persist repo → folder mapping | allow |
| `set_integration_default` | `{tool, value}` | persist default Jira project / Confluence space | allow |

### Memory & learning

| Tool | Args | Does | Gate |
|---|---|---|---|
| `memory_lookup` | `{query}` | unified recall (all sources, ranked) | RO |
| `memory_write` | `{text, kind, tags, scope}` | save a fact; `scope:"global"` = recalled everywhere | allow |
| `remember_rule` | `{text, description, triggers, scope}` | persist an always-on user rule | allow |
| `skill_search` / `learn_skill` | `{query}` / `{name, description, body, triggers, scope}` | find / author a reusable skill | RO / allow |
| `workflow_search` / `learn_workflow` | `{query}` / `{…, scripts: [{name, content, test}]}` | find / author a workflow; every script's test command is **actually run** before save (hard gate) | RO / allow |
| `create_job_script` | `{name, cron, script}` | save + schedule a recurring cron job | ASK |

### Progress

| Tool | Args | Does | Gate |
|---|---|---|---|
| `plan_progress` | `{slug, status: running\|done\|failed}` | flip a checklist item for multi-part asks (handled in the chat loop, not the registry) | allow |

## Pipeline Doer tools

The Doer's registry is `runtime/doer_tools.py` (typed `FunctionTool`s); what
the model may actually call is the `agents.yaml` allowlist:

- **Allowed** (`agents.yaml → doer.tools.allowed`): `editor`, `bash`,
  `file_read`, `file_write`, `file_patch`, `list_dir`, `run_shell`, `think`,
  `finish`, `graphify_lookup`, `memory_lookup`, `skill_search`, `learn_skill`,
  `repo_map`, `impacted_tests`, `grep_repo`, `serve`, `stop_service`,
  `subtask_update`, `git_commit`, plus Jira/Confluence **reads**
  (`jira_search/read/worklog/remote_links/transitions`,
  `confluence_search/read/children`).
- **Forbidden**: `ask_user`, `write_fact`, `write_plan`, `create_child_ticket`,
  `code_run`, `web_scan`, `web_execute_js`, `start_long_term_update`.
- Doer-only tools: `think` (traced no-op), `finish` (explicit termination —
  non-Doer callers get `agent_not_authorized` inside `tools/cognition.py`),
  `subtask_update`, `impacted_tests` (diff → covering tests), `memory_block`.
- Alias names a local model may emit (`read`, `write`, `bash`, `glob`,
  `todo_write`, `commit`, …) map to the canonical tools in `doer_tools.__all__`.

## Which agent gets which tools

From `aiforge_core/agents/agents.yaml` (enforced twice: the tool schema is
filtered per role before the agent boots — `agents/loader.tools_schema_for_role`
— and every call is re-checked at the tool boundary):

| Agent | Tools |
|---|---|
| **Chat** (simple/act) | the full 93-tool registry above |
| **Chat** (plan mode) | read-only subset only (`_READONLY_TOOLS`) — inspect, recall, all Jira/Confluence/GitLab/web **reads**, `context_gather`, resolvers; every mutating tool returns `blocked: plan_mode` |
| **Doer** | full build set above (edit + shell + verify + reads) |
| **Researcher** | repo reads + `memory_lookup`/`graphify_lookup` + the web trio (`web_search`, `web_read`, `web_crawl`) + Jira/Confluence reads; no writes/shell |
| **Architect** | `graphify_lookup`, `memory_lookup`, view-only `editor`, `grep_repo`, `repo_map`, `resolve_repo`, Jira/Confluence reads; no writes/shell |
| **Planner** | same read set as Architect + `jira_worklog`; plan-writing ops (`write_plan`, `create_child_ticket`) are server-side, not model tools |
| **Live-verifier** | `bash`, `file_read`, `grep` (runs the real recipe; can't edit) |
| **Ctx fan-out** (`ctx_memory` / `ctx_repomap` / `ctx_conventions`) | narrow read sets (`memory_lookup` / repo-map + grep + view-editor / grep + view-editor) |
| **Enhancer / gap_eval** | no allowlist, but all write/exec tools forbidden |
| **Verifier, Refiner, Feedback, Learner, Triage, Validator, verify_*** | tool-less (`forbidden: ALL`) — pure text stages |

`editor` is view-only for Architect/Planner/Researcher/ctx roles via
`editor_commands: [view]` (enforced inside `tools/editor.py`).

## Scripts: the `aiforge-tool` CLI

Job and workflow scripts never hand-roll `curl`: `aiforge-tool <tool_name>
'<json args>'` dispatches into the same chat registry with the configured
integration (URL + auth from Settings). Read-only tools only, by default;
`AIFORGE_TOOL_CLI_ALLOW_WRITES=1` for operator-audited jobs
(`runtime/tool_cli.py`). `aiforge-tool --list` enumerates.

## Graphify graph

`graphify` output (`<repo>/graphify-out/graph.json`) is loaded into Neo4j by
`aiforge_core/indexing/graphify_loader.py` (idempotent upserts) and queried by
the `graphify_lookup` tool (Architect/Planner/Doer/Researcher/ctx_repomap).
Refresh scripts: `scripts/runtime/aiforge-graphify-all.sh`,
`scripts/runtime/graphify-nightly.sh`, install via `run.sh --with-graphify`.
