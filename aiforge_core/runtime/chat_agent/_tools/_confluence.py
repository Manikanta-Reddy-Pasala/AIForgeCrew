from __future__ import annotations


def _t_confluence_search(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_search(args, cwd)


def _t_confluence_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_read(args, cwd)


def _t_confluence_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_create(args, cwd)


def _t_confluence_update(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_update(args, cwd)


def _t_confluence_attach(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_attach(args, cwd)


def _t_confluence_resolve_space(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_resolve_space(args, cwd)


def _t_confluence_children(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_children(args, cwd)


def _t_confluence_spaces(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_spaces(args, cwd)


def _t_confluence_page_by_title(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_page_by_title(args, cwd)


def _t_confluence_labels(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_labels(args, cwd)


def _t_confluence_add_label(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_add_label(args, cwd)


def _t_confluence_comments(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_comments(args, cwd)


def _t_confluence_comment(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_comment(args, cwd)


def _t_confluence_descendants(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_descendants(args, cwd)
