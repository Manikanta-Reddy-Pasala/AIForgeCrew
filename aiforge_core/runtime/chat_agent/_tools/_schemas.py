"""OpenAI-native function schemas for the chat agent's tools — ALL of them.

The text ACTION/ARGS_JSON protocol makes local models fumble arguments into
``ARGS_JSON: {}``. Native OpenAI tool-calling (what OpenWebUI uses) returns real
structured arguments instead. Every tool in the ``TOOLS`` registry gets a native
schema: a rich, described one from :data:`CATALOG` where the arg shape is known,
or a permissive fallback (``additionalProperties: true``) otherwise — so a new
registry tool is automatically exposed natively too.

Schema names MUST equal the ``TOOLS`` registry keys so a native ``tool_calls``
reply dispatches through the exact same path as a text ACTION.
"""
from __future__ import annotations

# Compact type DSL → JSON-schema property fragment.
_T = {
    "s": {"type": "string"},
    "i": {"type": "integer"},
    "b": {"type": "boolean"},
    "arrs": {"type": "array", "items": {"type": "string"}},
    "arr": {"type": "array"},
    "obj": {"type": "object"},
}

# name -> (description, {prop: type_code}, (required, …))
# Arg shapes mirror the system-prompt tool catalog (_prompt.py). Every property
# is optional at the schema level except those in the required tuple; the object
# stays open (additionalProperties) so an extra documented key still passes.
CATALOG: dict = {
    # ── files / editing ──────────────────────────────────────────────────
    "file_read": ("Read a file's contents.", {"path": "s"}, ("path",)),
    "read_files": ("Read MANY files in ONE call (batched file_read). Pass a "
                   "`paths` list — ALWAYS prefer this over reading files one at "
                   "a time when you need several.", {"paths": "arrs"}, ("paths",)),
    "file_write": ("Create/overwrite a file (syntax-checked; force:true "
                   "overrides).", {"path": "s", "content": "s", "force": "b"},
                   ("path", "content")),
    "file_create": ("Create a new file.", {"path": "s", "content": "s"},
                    ("path", "content")),
    "file_patch": ("Replace old_text with new_text in a file.",
                   {"path": "s", "old_text": "s", "new_text": "s", "force": "b"},
                   ("path", "old_text", "new_text")),
    "multi_edit": ("Apply several find/replace edits across one or many files "
                   "atomically.", {"edits": "arr"}, ("edits",)),
    "editor": ("Structured editor with syntax-check + undo. command: view|"
               "create|str_replace|insert|undo_edit.",
               {"command": "s", "path": "s", "old_str": "s", "new_str": "s",
                "file_text": "s", "insert_line": "i"}, ("command", "path")),
    "list_dir": ("List a directory.", {"path": "s"}, ("path",)),
    "find": ("Fuzzy-locate files/dirs by partial name.",
             {"name": "s", "kind": "s"}, ("name",)),
    "grep": ("Recursively search file contents.", {"pattern": "s", "path": "s"},
             ("pattern",)),
    "read_lines": ("Read a line range from a file.",
                   {"path": "s", "start": "i", "end": "i"}, ("path",)),
    "summarize_doc": ("Summarize an attached/loaded document (pdf/docx/xlsx). "
                      "Give the file name/path. Optional `pages` selects a page "
                      "range to summarize ONLY those pages/sections — e.g. "
                      "\"10-20\" or \"3,5,7-9\"; omit for the whole document. "
                      "Handles tables, embedded images, and OCRs scanned PDFs.",
                      {"path": "s", "pages": "s"}, ("path",)),
    "format": ("Auto-format a file.", {"path": "s"}, ()),
    "rename_symbol": ("Rename a symbol across the project.",
                      {"path": "s", "old_name": "s", "new_name": "s"}, ()),
    "lsp": ("Symbol navigation: goto_definition|find_references|hover.",
            {"command": "s", "path": "s", "line": "i", "character": "i"},
            ("command", "path")),
    # ── run / build / test ───────────────────────────────────────────────
    "run_command": ("Run a shell command (timeout SECONDS, default 600).",
                    {"cmd": "s", "timeout": "i"}, ("cmd",)),
    "watch_until": (
        "Re-run one command until a condition holds — polling, monitoring, "
        "'wait until it's ready/done/green'. The loop is code: ONE call covers "
        "the whole watch (do NOT hand-roll a poll loop with run_command). "
        "Derive interval_s/max_checks/timeout_s from what the user asked for — "
        "'monitor for 10 minutes every 15 seconds' is interval_s=15, "
        "max_checks=40, timeout_s=600. until: exit_zero (default) | "
        "exit_nonzero | contains:TEXT | not_contains:TEXT | regex:PATTERN.",
        {"cmd": "s", "until": "s", "interval_s": "i", "max_checks": "i",
         "timeout_s": "i", "cmd_timeout": "i"}, ("cmd",)),
    "schedule_task": (
        "Run an instruction LATER and REPEATEDLY on a schedule (it outlives "
        "this chat; each run files a ticket). action: create | list | cancel. "
        "Give `cron` (5-field) or `every_minutes`. For waiting on something "
        "NOW, use watch_until instead.",
        {"action": "s", "name": "s", "instruction": "s", "cron": "s",
         "every_minutes": "i", "job_id": "i", "project": "s"}, ()),
    "project": ("Detect + build/test/run the project.", {"action": "s"},
                ("action",)),
    "ensure_runtime": ("Install + verify missing toolchain binaries.",
                       {"tools": "arrs"}, ("tools",)),
    "run_tests": ("Run the project's tests. mode fast|all|discover.",
                  {"mode": "s", "pattern": "s"}, ()),
    "typecheck": ("Run the project's type-checker.", {}, ()),
    "serve": ("Start a server/app in the background.", {"cmd": "s", "port": "i"},
              ("cmd",)),
    "stop_service": ("Stop a service started with serve.", {"pid": "i"},
                     ("pid",)),
    "list_services": ("List background services you started.", {}, ()),
    "execute_ipython_cell": ("Run a Python/IPython code cell.", {"code": "s"},
                             ("code",)),
    # ── git ──────────────────────────────────────────────────────────────
    "git_status": ("Git working-tree status.", {"path": "s"}, ()),
    "git_diff": ("Git diff.", {"path": "s"}, ()),
    "git_log": ("Recent git commits.", {"path": "s", "limit": "i"}, ()),
    "git_blame": ("Git blame for a file.", {"path": "s"}, ("path",)),
    "github_pr": ("Open a GitHub PR from the current branch.",
                  {"title": "s", "body": "s", "base": "s", "draft": "b"}, ()),
    # ── memory / knowledge / skills ──────────────────────────────────────
    "memory_lookup": ("Recall learnings/decisions from memory.", {"query": "s"},
                      ("query",)),
    "memory_write": ("Persist a durable fact/decision.",
                     {"text": "s", "kind": "s", "scope": "s", "tags": "arrs"},
                     ("text",)),
    "search_chat_sessions": ("Find things discussed in PAST chat sessions.",
                             {"query": "s", "limit": "i"}, ("query",)),
    "remember_rule": ("Persist a user rule for every session.",
                      {"text": "s", "description": "s", "scope": "s",
                       "triggers": "arrs"}, ("text",)),
    "skill_search": ("Find reusable SKILL.md playbooks.", {"query": "s"},
                     ("query",)),
    "learn_skill": ("Author a reusable skill.",
                    {"name": "s", "description": "s", "body": "s",
                     "triggers": "arrs", "scope": "s"}, ("name", "body")),
    "workflow_search": ("Find reusable WORKFLOW.md procedures.", {"query": "s"},
                        ("query",)),
    "learn_workflow": ("Author a reusable multi-step workflow.",
                       {"name": "s", "description": "s", "body": "s",
                        "triggers": "arrs", "scope": "s", "scripts": "arr"},
                       ("name", "body")),
    "create_job_script": ("Save + schedule a recurring cron job script.",
                          {"name": "s", "cron": "s", "script": "s",
                           "description": "s"}, ("name", "script")),
    "note_curate": ("Re-verify a saved note against its live source.",
                    {"path": "s"}, ()),
    "note_consolidate": ("Merge new knowledge into a note's OKR sections.",
                         {"text": "s", "path": "s"}, ("text",)),
    "context_gather": ("Pull an entity + its linked pages/tickets/images in "
                       "parallel into a dossier.", {"kind": "s", "key": "s"},
                       ("kind", "key")),
    # ── repo / config resolution ─────────────────────────────────────────
    "resolve_repo": ("Loose repo/service name → local path.", {"name": "s"},
                     ("name",)),
    "set_integration_default": ("Persist a default project/space.",
                                {"tool": "s", "value": "s"}, ("tool", "value")),
    "set_repo_folder": ("Persist the local folder for a repo.",
                        {"repo": "s", "path": "s"}, ("repo", "path")),
    "set_repo_root": ("Persist the global base folder for all repos.",
                      {"path": "s"}, ("path",)),
    "list_repos": ("Show configured repo folders.", {}, ()),
    # ── code graph ───────────────────────────────────────────────────────
    "codegraph_query": ("Query the code graph.",
                        {"query": "s", "symbol": "s"}, ()),
    "codegraph_callers": ("Who calls this symbol.",
                          {"symbol": "s", "path": "s"}, ()),
    "codegraph_callees": ("What this symbol calls.",
                          {"symbol": "s", "path": "s"}, ()),
    "codegraph_impact": ("Blast radius of changing a symbol.",
                         {"symbol": "s", "path": "s"}, ()),
    "codegraph_explore": ("Explore the code graph around a symbol/topic.",
                          {"query": "s", "symbol": "s"}, ()),
    # ── Jira ─────────────────────────────────────────────────────────────
    "jira_search": ("READ: find/list Jira issues (text query or jql). Use this for 'get/show/list my tickets'.",
                    {"query": "s", "jql": "s", "limit": "s", "time": "b"}, ()),
    "jira_read": ("READ one Jira issue by key: fields, description, comments, attachments.", {"key": "s"}, ("key",)),
    "jira_worklog": ("All time logged on an issue.", {"key": "s"}, ("key",)),
    "jira_log_work": ("Record time against an issue.",
                      {"key": "s", "time_spent": "s", "comment": "s"},
                      ("key", "time_spent")),
    "jira_remote_links": ("Confluence pages + web links on an issue.",
                          {"key": "s"}, ("key",)),
    "jira_create": ("WRITE: create a NEW Jira issue. Only when explicitly asked to create/file/raise one — never to look tickets up.",
                    {"project": "s", "summary": "s", "issuetype": "s",
                     "description": "s"}, ("project", "summary")),
    "jira_update": ("WRITE: edit an existing Jira issue's fields.",
                    {"key": "s", "summary": "s", "description": "s",
                     "labels": "arrs", "status": "s"}, ("key",)),
    "jira_comments": ("READ the comments on a Jira issue.",
                      {"key": "s", "limit": "s"}, ("key",)),
    "jira_comment": ("WRITE: post a NEW comment onto a Jira issue. To read comments use jira_comments.", {"key": "s", "body": "s"},
                     ("key", "body")),
    "jira_transition": ("Move a Jira issue's status.",
                        {"key": "s", "transition": "s"}, ("key", "transition")),
    "jira_transitions": ("List available transitions for an issue.",
                         {"key": "s"}, ("key",)),
    "jira_assign": ("Assign a Jira issue.", {"key": "s", "assignee": "s"},
                    ("key",)),
    "jira_resolve_project": ("Loose project name → Jira project key.",
                             {"name": "s"}, ("name",)),
    "jira_myself": ("The authenticated Jira user.", {}, ()),
    "jira_projects": ("List Jira projects.", {}, ()),
    "jira_boards": ("List Agile boards.", {"project": "s"}, ()),
    "jira_sprints": ("List sprints on a board.",
                     {"board_id": "i", "state": "s"}, ()),
    "jira_sprint_issues": ("Issues in a sprint.",
                           {"sprint_id": "i", "time": "b"}, ()),
    "jira_dashboards": ("List Jira dashboards.", {}, ()),
    "jira_dashboard_read": ("Read a dashboard + gadgets.", {"id": "i"}, ("id",)),
    "jira_dashboard_create": ("Create a dashboard.",
                              {"name": "s", "description": "s", "share": "s"},
                              ("name",)),
    # ── Confluence ───────────────────────────────────────────────────────
    "confluence_search": ("READ: find/list Confluence pages (text query or cql).",
                          {"query": "s", "cql": "s"}, ()),
    "confluence_read": ("READ one Confluence page (id, or title+space).",
                        {"id": "s", "title": "s", "space": "s"}, ()),
    "confluence_create": ("WRITE: create a NEW Confluence page. Only when explicitly asked to create one — never to look pages up.",
                          {"title": "s", "space": "s", "body": "s",
                           "parent_id": "s"}, ("title", "space", "body")),
    "confluence_update": ("WRITE: edit an existing Confluence page.",
                          {"id": "s", "body": "s", "title": "s"},
                          ("id", "body")),
    "confluence_attach": ("Upload a file as a page attachment.",
                          {"id": "s", "path": "s", "url": "s"}, ("id",)),
    "confluence_children": ("Direct child pages.", {"id": "s"}, ("id",)),
    "confluence_descendants": ("All descendant pages.", {"id": "s"}, ("id",)),
    "confluence_spaces": ("List spaces.", {}, ()),
    "confluence_page_by_title": ("Find a page id by exact title.",
                                 {"space": "s", "title": "s"},
                                 ("space", "title")),
    "confluence_labels": ("Read a page's labels.", {"id": "s"}, ("id",)),
    "confluence_add_label": ("Add labels to a page.",
                             {"id": "s", "labels": "arrs"}, ("id", "labels")),
    "confluence_comments": ("READ the comments on a Confluence page.",
                            {"id": "s"}, ("id",)),
    "confluence_comment": ("WRITE: post a NEW comment onto a page. To read comments use confluence_comments.", {"id": "s", "body": "s"},
                           ("id", "body")),
    "confluence_resolve_space": ("Loose space name → space key.",
                                 {"name": "s"}, ("name",)),
    # ── GitLab ───────────────────────────────────────────────────────────
    "gitlab_search": ("READ: find/list GitLab issues.",
                      {"query": "s", "project": "s", "state": "s"}, ()),
    "gitlab_read": ("READ one GitLab issue: fields + comments.",
                    {"project": "s", "iid": "i"},
                    ("project", "iid")),
    "gitlab_create": ("WRITE: create a NEW GitLab issue. Only when explicitly asked to create one — never to look issues up.",
                      {"project": "s", "title": "s", "description": "s",
                       "labels": "arrs"}, ("project", "title")),
    "gitlab_update": ("WRITE: edit an existing GitLab issue.",
                      {"project": "s", "iid": "i", "title": "s",
                       "labels": "arrs", "state_event": "s"}, ("project", "iid")),
    "gitlab_comment": ("WRITE: post a NEW comment onto a GitLab issue.",
                       {"project": "s", "iid": "i", "body": "s"},
                       ("project", "iid", "body")),
    "gitlab_mr_create": ("WRITE: open a NEW GitLab merge request.",
                         {"project": "s", "source_branch": "s",
                          "target_branch": "s", "title": "s", "description": "s"},
                         ("project", "source_branch", "target_branch", "title")),
    "gitlab_mr_comment": ("WRITE: post a NEW comment onto a GitLab MR.",
                          {"project": "s", "iid": "i", "body": "s"},
                          ("project", "iid", "body")),
    # ── email ────────────────────────────────────────────────────────────
    "email_send": ("Send an email via SMTP.",
                   {"to": "s", "subject": "s", "body": "s", "cc": "s",
                    "bcc": "s", "html": "b"}, ("to", "subject", "body")),
    "email_read": ("Read recent inbox emails via IMAP.",
                   {"query": "s", "limit": "i", "folder": "s",
                    "unseen_only": "b"}, ()),
    # ── web ──────────────────────────────────────────────────────────────
    "web_search": ("Search the open web.", {"query": "s", "limit": "i"},
                   ("query",)),
    "web_fetch": ("Read a page's text.", {"url": "s", "max_chars": "i"},
                  ("url",)),
    "web_crawl": ("Fetch a page as markdown + save to the web dossier.",
                  {"url": "s"}, ("url",)),
    # ── delegation / mcp / browser ───────────────────────────────────────
    "delegate_to_agent": ("Delegate a sub-task to another agent.",
                          {"task": "s", "agent": "s"}, ("task",)),
    "delegate": ("Delegate a sub-task to another agent.",
                 {"task": "s", "agent": "s"}, ("task",)),
    "mcp": ("Call an MCP tool.",
            {"server": "s", "tool": "s", "args": "obj"}, ()),
    "browse": ("Drive a headless browser.", {"url": "s", "action": "s"}, ()),
}


def _fn(name: str, desc: str, props: dict, required: tuple) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {
            "type": "object",
            "properties": {k: _T.get(v, _T["s"]) for k, v in props.items()},
            "required": list(required), "additionalProperties": True}}}


def _permissive(name: str) -> dict:
    """Fallback schema for a tool whose arg shape isn't catalogued — the object
    stays fully open so the model supplies the args it knows from the prompt's
    tool catalog. Guarantees EVERY registry tool is callable natively."""
    return {"type": "function", "function": {
        "name": name,
        "description": f"{name} tool — see the system prompt's tool catalog for "
                       "its arguments.",
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": True}}}


def _registry_names() -> list[str]:
    """All tool names from the live registry (so a newly-added tool is auto-
    exposed). Falls back to the catalog keys if the registry can't be imported."""
    try:
        from .._registry import TOOLS
        return list(TOOLS)
    except Exception:  # noqa: BLE001 — never let schema-build break a turn
        return list(CATALOG)


def _build() -> list[dict]:
    out: list[dict] = []
    for name in _registry_names():
        if name in CATALOG:
            desc, props, req = CATALOG[name]
            out.append(_fn(name, desc, props, req))
        else:
            out.append(_permissive(name))
    return out


NATIVE_TOOL_SCHEMAS: list[dict] = _build()
NATIVE_TOOL_NAMES = frozenset(s["function"]["name"] for s in NATIVE_TOOL_SCHEMAS)
