from __future__ import annotations


def _t_jira_search(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_search(args, cwd)


def _t_jira_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_read(args, cwd)


def _t_jira_worklog(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_worklog(args, cwd)


def _t_jira_remote_links(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_remote_links(args, cwd)


def _t_jira_resolve_project(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_resolve_project(args, cwd)


def _t_jira_log_work(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_log_work(args, cwd)


def _t_jira_myself(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_myself(args, cwd)


def _t_jira_projects(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_projects(args, cwd)


def _t_jira_boards(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_boards(args, cwd)


def _t_jira_sprints(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_sprints(args, cwd)


def _t_jira_sprint_issues(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_sprint_issues(args, cwd)


def _t_jira_dashboards(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_dashboards(args, cwd)


def _t_jira_dashboard_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_dashboard_read(args, cwd)


def _t_jira_dashboard_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_dashboard_create(args, cwd)


def _t_jira_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_create(args, cwd)


def _t_jira_update(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_update(args, cwd)


def _t_jira_comment(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_comment(args, cwd)


def _t_jira_transitions(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_transitions(args, cwd)


def _t_jira_transition(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_transition(args, cwd)


def _t_jira_assign(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_assign(args, cwd)


def _t_jira_link_issues(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_link_issues(args, cwd)
