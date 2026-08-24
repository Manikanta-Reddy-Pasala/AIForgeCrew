"""External-integration tools (typed FunctionTool wrappers): Confluence,
JIRA, GitLab, email, cross-entity context_gather, and repo/project name
resolution.

These delegate to the same REST clients the chat agent uses
(aiforge_core.runtime.tools.{confluence,jira,gitlab,email_tool}). They live
here as typed wrappers so ADK FunctionTool can advertise them to the pipeline
Doer — the chat-side fn(args, cwd) signatures cannot be wrapped directly. The
pipeline's before_tool_callback + tool_policy apply the ASK/approval gate by
name.

Split out of the former ``doer_tools`` module — moved verbatim.
"""
from __future__ import annotations

from ..sandbox import root


def confluence_search(query: str = "", cql: str = "", limit: int = 10) -> dict:
    """Find Confluence pages. Pass ``query`` (full-text) OR ``cql`` (raw CQL)."""
    from aiforge_core.runtime.tools import confluence as _c
    return _c.confluence_search({"query": query, "cql": cql, "limit": limit}, str(root()))


def confluence_read(id: str = "", title: str = "", space: str = "") -> dict:
    """Read a Confluence page (storage XHTML body) by ``id`` or ``title`` (+``space``)."""
    from aiforge_core.runtime.tools import confluence as _c
    return _c.confluence_read({"id": id, "title": title, "space": space}, str(root()))


def confluence_create(title: str, space: str, body: str, parent_id: str = "") -> dict:
    """Create a Confluence page. ``body`` is storage XHTML. ``space`` = space key."""
    from aiforge_core.runtime.tools import confluence as _c
    return _c.confluence_create({"title": title, "space": space, "body": body,
                                 "parent_id": parent_id}, str(root()))


def confluence_update(id: str, body: str, title: str = "") -> dict:
    """Update a Confluence page body (version auto-incremented). ``id`` required."""
    from aiforge_core.runtime.tools import confluence as _c
    return _c.confluence_update({"id": id, "body": body, "title": title}, str(root()))


def jira_search(query: str = "", jql: str = "", limit=50) -> dict:
    """Find JIRA issues. Pass ``query`` (full-text) OR ``jql`` (raw JQL).
    ``limit`` defaults to 50; pass ``limit="all"`` to pull every match
    (paginated) up to the safety cap."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_search({"query": query, "jql": jql, "limit": limit}, str(root()))


def jira_read(key: str) -> dict:
    """Read a JIRA issue by ``key`` (e.g. ENG-123). Returns fields, comments,
    and time tracking (original/remaining estimate + time spent)."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_read({"key": key}, str(root()))


def jira_worklog(key: str, limit: int = 50) -> dict:
    """Time LOGGED on a JIRA issue: every worklog (who/how much/when) + the
    estimate/spent rollup. Use for "how much time is recorded on ENG-123"."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_worklog({"key": key, "limit": limit}, str(root()))


def resolve_repo(name: str) -> dict:
    """Fuzzily resolve a repo/service/folder NAME to its local path (tolerates
    case, spaces, missing hyphens, typos) — call before assuming a path."""
    from aiforge_core.config import repo_map as _rm
    return _rm.resolve(name or "")


def jira_remote_links(key: str) -> dict:
    """Confluence pages + web links attached to a JIRA issue."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_remote_links({"key": key}, str(root()))


def resolve_repo(name: str) -> dict:
    """Resolve a loosely-typed repo/service/folder name to its local path
    (tolerates case, spaces, missing hyphens, typos)."""
    from aiforge_core.config import repo_map
    return repo_map.resolve(name)


def jira_resolve_project(name: str) -> dict:
    """Loose Jira project name → real project key."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_resolve_project({"name": name}, str(root()))


def confluence_resolve_space(name: str) -> dict:
    """Loose Confluence space name → real space key."""
    from aiforge_core.runtime.tools import confluence as _c
    return _c.confluence_resolve_space({"name": name}, str(root()))


def context_gather(kind: str, key: str, force: bool = False) -> dict:
    """Assemble a cross-entity dossier (a Jira ticket + its linked Confluence
    pages + images, or a page + its tickets) IN PARALLEL, cached + refreshed."""
    from aiforge_core.runtime import context_gather as _cg
    return _cg.gather(kind, key, force=force, role="doer")


def jira_log_work(key: str, time_spent: str, comment: str = "") -> dict:
    """Record time against a JIRA issue. ``time_spent`` is a Jira duration
    (e.g. '2h 30m', '1d')."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_log_work({"key": key, "time_spent": time_spent,
                             "comment": comment}, str(root()))


def jira_myself() -> dict:
    """The authenticated JIRA user (resolve "me"/"my")."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_myself({}, str(root()))


def jira_projects(limit: int = 50) -> dict:
    """List JIRA projects the token can see (key, name, lead)."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_projects({"limit": limit}, str(root()))


def jira_boards(project: str = "", limit: int = 50) -> dict:
    """List Agile boards, optionally for one ``project``."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_boards({"project": project, "limit": limit}, str(root()))


def jira_sprints(board_id: str, state: str = "", limit: int = 50) -> dict:
    """List sprints on a board. ``state`` = active|closed|future (optional)."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_sprints({"board_id": board_id, "state": state,
                            "limit": limit}, str(root()))


def jira_sprint_issues(sprint_id: str, time: bool = False,
                       limit: int = 50) -> dict:
    """Issues in a sprint. ``time`` adds estimate/spent per issue."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_sprint_issues({"sprint_id": sprint_id, "time": time,
                                  "limit": limit}, str(root()))


def jira_dashboards(limit: int = 50) -> dict:
    """List JIRA dashboards visible to the token."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_dashboards({"limit": limit}, str(root()))


def jira_dashboard_read(id: str) -> dict:
    """Read one JIRA dashboard + its gadgets by ``id``."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_dashboard_read({"id": id}, str(root()))


def jira_dashboard_create(name: str, description: str = "",
                          share: str = "private") -> dict:
    """Create a JIRA dashboard (Cloud). ``share`` = private|authenticated|global."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_dashboard_create({"name": name, "description": description,
                                     "share": share}, str(root()))


def jira_create(project: str, summary: str, description: str = "",
                issuetype: str = "Task", labels: str = "") -> dict:
    """Create a JIRA issue. ``project`` = project key. ``labels`` = comma-separated."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_create({"project": project, "summary": summary,
                           "description": description, "issuetype": issuetype,
                           "labels": labels}, str(root()))


def jira_update(key: str, summary: str = "", description: str = "",
                labels: str = "", status: str = "") -> dict:
    """Update JIRA issue fields by ``key``. ``labels`` = comma-separated.
    ``status`` moves the issue through its workflow (auto-routed to a
    transition — Jira status is not a plain editable field)."""
    from aiforge_core.runtime.tools import jira as _j
    args: dict = {"key": key}
    if summary:
        args["summary"] = summary
    if description:
        args["description"] = description
    if labels:
        args["labels"] = labels
    if status:
        args["status"] = status
    return _j.jira_update(args, str(root()))


def jira_comment(key: str, body: str) -> dict:
    """Add a comment to a JIRA issue by ``key``."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_comment({"key": key, "body": body}, str(root()))


# ─── Email (SMTP send / IMAP read) — same soft-fail + config-gate as JIRA ─

def email_send(to: str = "", subject: str = "", body: str = "",
               cc: str = "", bcc: str = "", html: str = "") -> dict:
    """Send an email via the configured SMTP server. ``to``/``cc``/``bcc`` =
    comma-separated. Soft-fails (unconfigured ⇒ ``ok: False``)."""
    from aiforge_core.runtime.tools import email_tool as _e
    return _e.email_send({"to": to, "subject": subject, "body": body,
                          "cc": cc, "bcc": bcc, "html": html}, str(root()))


def email_read(folder: str = "INBOX", limit: int = 10, query: str = "",
               unseen_only: bool = False) -> dict:
    """Read recent/matching inbox emails via the configured IMAP server.
    ``query`` matches subject+from; ``unseen_only`` limits to unread."""
    from aiforge_core.runtime.tools import email_tool as _e
    return _e.email_read({"folder": folder, "limit": limit, "query": query,
                          "unseen_only": unseen_only}, str(root()))


# ─── GitLab (issues + MRs) — same soft-fail + config-gate as JIRA ────────
# gitlab.py implemented these but they were never wrapped as FunctionTools, so
# agents could use Jira/Confluence/Email but NOT GitLab even when it was
# configured in the UI. Wire the full read+write surface here.

def gitlab_search(query: str = "", limit: int = 20, state: str = "all",
                  labels: str = "") -> dict:
    """Search GitLab issues for the configured project(s). ``state`` ∈
    opened|closed|all. ``labels`` = comma-separated. Read-only, soft-fails."""
    from aiforge_core.runtime.tools import gitlab as _g
    return _g.gitlab_search({"query": query, "limit": limit, "state": state,
                             "labels": labels}, str(root()))


def gitlab_read(project: str, iid: str) -> dict:
    """Read one GitLab issue (with comments) by ``project`` + issue ``iid``."""
    from aiforge_core.runtime.tools import gitlab as _g
    return _g.gitlab_read({"project": project, "iid": iid}, str(root()))


def gitlab_pipelines(project: str = "", ref: str = "", status: str = "",
                     sha: str = "", limit: int = 20) -> dict:
    """List recent GitLab CI pipelines, newest first. ``ref`` = branch/tag.
    Read-only, soft-fails."""
    from aiforge_core.runtime.tools import gitlab as _g
    return _g.gitlab_pipelines({"project": project, "ref": ref, "sha": sha,
                                "status": status, "limit": limit}, str(root()))


def gitlab_pipeline(project: str = "", pipeline_id: int = 0, ref: str = "",
                    sha: str = "", logs: bool = True,
                    log_chars: int = 3000) -> dict:
    """One GitLab CI pipeline: status, jobs, and the log tail of what failed.
    Give ``pipeline_id``, or ``ref`` (latest on that branch), or ``sha`` — or
    none of them for the project's latest. A SNAPSHOT; to wait for the outcome
    use ``gitlab_pipeline_watch``."""
    from aiforge_core.runtime.tools import gitlab as _g
    return _g.gitlab_pipeline({"project": project, "pipeline_id": pipeline_id,
                               "ref": ref, "sha": sha, "logs": logs,
                               "log_chars": log_chars}, str(root()))


def gitlab_pipeline_watch(project: str = "", pipeline_id: int = 0,
                          ref: str = "", sha: str = "", timeout_s: int = 600,
                          interval_s: int = 20, max_checks: int = 60) -> dict:
    """Watch a GitLab CI pipeline until it finishes, then report whether it
    passed and why it failed. ONE call covers the whole watch — never poll
    ``gitlab_pipeline`` in a loop.

    The budget is clamped to ~180s / 10 checks ONLY when nothing can interrupt
    the watch (the jobs runner, ``/api/chat/agent``). Team mode and subtask
    runs re-bind the session in their driver thread — ``chat_cancel.set_active``
    — precisely so Stop reaches the tools they run, so there the full
    ``timeout_s`` applies. When the clamp did apply, the effective value comes
    back as ``unattended_budget_s``; a build longer than the budget returns
    ``timed_out`` with the last status seen, never an invented outcome."""
    from aiforge_core.runtime.tools import gitlab as _g
    return _g.gitlab_pipeline_watch(
        {"project": project, "pipeline_id": pipeline_id, "ref": ref,
         "sha": sha, "timeout_s": timeout_s, "interval_s": interval_s,
         "max_checks": max_checks},
        str(root()))


def gitlab_create(project: str, title: str, description: str = "",
                  labels: str = "") -> dict:
    """Create a GitLab issue. ``labels`` = comma-separated."""
    from aiforge_core.runtime.tools import gitlab as _g
    return _g.gitlab_create({"project": project, "title": title,
                             "description": description, "labels": labels},
                            str(root()))


def gitlab_update(project: str, iid: str, title: str = "", description: str = "",
                  labels: str = "", state_event: str = "") -> dict:
    """Update a GitLab issue. ``state_event`` ∈ close|reopen. ``labels`` =
    comma-separated."""
    from aiforge_core.runtime.tools import gitlab as _g
    args: dict = {"project": project, "iid": iid}
    if title:
        args["title"] = title
    if description:
        args["description"] = description
    if labels:
        args["labels"] = labels
    if state_event:
        args["state_event"] = state_event
    return _g.gitlab_update(args, str(root()))


def gitlab_comment(project: str, iid: str, body: str) -> dict:
    """Add a comment to a GitLab issue by ``project`` + ``iid``."""
    from aiforge_core.runtime.tools import gitlab as _g
    return _g.gitlab_comment({"project": project, "iid": iid, "body": body},
                             str(root()))


def gitlab_mr_create(project: str, source_branch: str, title: str,
                     target_branch: str = "main", description: str = "",
                     labels: str = "") -> dict:
    """Open a GitLab merge request. ``labels`` = comma-separated."""
    from aiforge_core.runtime.tools import gitlab as _g
    return _g.gitlab_mr_create({"project": project, "source_branch": source_branch,
                                "target_branch": target_branch, "title": title,
                                "description": description, "labels": labels},
                               str(root()))


def gitlab_mr_comment(project: str, iid: str, body: str) -> dict:
    """Add a comment to a GitLab merge request by ``project`` + MR ``iid``."""
    from aiforge_core.runtime.tools import gitlab as _g
    return _g.gitlab_mr_comment({"project": project, "iid": iid, "body": body},
                                str(root()))


# ─── Jira workflow (transition / assign) ────────────────────────────────

def jira_transitions(key: str) -> dict:
    """List the workflow transitions available for a Jira issue (id + name +
    target status)."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_transitions({"key": key}, str(root()))


def jira_transition(key: str, transition: str, comment: str = "") -> dict:
    """Move a Jira issue through its workflow (e.g. In Progress → Done).
    ``transition`` = a transition id, its name, or the target status name."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_transition({"key": key, "transition": transition,
                               "comment": comment}, str(root()))


def jira_assign(key: str, assignee: str) -> dict:
    """Assign a Jira issue to a user (``"-1"`` / ``"unassigned"`` clears it)."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_assign({"key": key, "assignee": assignee}, str(root()))


def jira_link_issues(inward: str, outward: str, type: str = "Relates",
                     comment: str = "") -> dict:
    """Link two Jira issues. ``type`` = link-type name (Blocks/Relates/…);
    semantics: inward <type> outward."""
    from aiforge_core.runtime.tools import jira as _j
    return _j.jira_link_issues({"inward": inward, "outward": outward,
                                "type": type, "comment": comment}, str(root()))


def confluence_children(id: str, limit: int = 50) -> dict:
    """List the child pages of a Confluence page by ``id``."""
    from aiforge_core.runtime.tools import confluence as _c
    return _c.confluence_children({"id": id, "limit": limit}, str(root()))


def confluence_attach(id: str, path: str) -> dict:
    """Attach a local file (``path``) to a Confluence page (``id``)."""
    from aiforge_core.runtime.tools import confluence as _c
    return _c.confluence_attach({"id": id, "path": path}, str(root()))


def confluence_spaces(limit: int = 50) -> dict:
    """List Confluence spaces the token can see."""
    from aiforge_core.runtime.tools import confluence as _c
    return _c.confluence_spaces({"limit": limit}, str(root()))


def confluence_page_by_title(space: str, title: str) -> dict:
    """Find a page by exact ``title`` in a ``space`` — returns id + version."""
    from aiforge_core.runtime.tools import confluence as _c
    return _c.confluence_page_by_title({"space": space, "title": title},
                                       str(root()))


def confluence_labels(id: str) -> dict:
    """Read the labels on a Confluence page."""
    from aiforge_core.runtime.tools import confluence as _c
    return _c.confluence_labels({"id": id}, str(root()))


def confluence_add_label(id: str, labels: str) -> dict:
    """Add labels (comma-separated) to a Confluence page."""
    from aiforge_core.runtime.tools import confluence as _c
    return _c.confluence_add_label({"id": id, "labels": labels}, str(root()))


def confluence_comments(id: str, limit: int = 25) -> dict:
    """Read the comments on a Confluence page."""
    from aiforge_core.runtime.tools import confluence as _c
    return _c.confluence_comments({"id": id, "limit": limit}, str(root()))


def confluence_comment(id: str, body: str) -> dict:
    """Add a comment to a Confluence page."""
    from aiforge_core.runtime.tools import confluence as _c
    return _c.confluence_comment({"id": id, "body": body}, str(root()))


def confluence_descendants(id: str, limit: int = 100) -> dict:
    """List ALL descendant pages of a Confluence page (deep)."""
    from aiforge_core.runtime.tools import confluence as _c
    return _c.confluence_descendants({"id": id, "limit": limit}, str(root()))
