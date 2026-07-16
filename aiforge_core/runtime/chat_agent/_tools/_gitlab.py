from __future__ import annotations

import subprocess


def _t_gitlab_search(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_search(args, cwd)


def _t_gitlab_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_read(args, cwd)


def _t_gitlab_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_create(args, cwd)


def _t_gitlab_update(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_update(args, cwd)


def _t_gitlab_comment(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_comment(args, cwd)


def _t_gitlab_mr_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_mr_create(args, cwd)


def _t_gitlab_mr_comment(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_mr_comment(args, cwd)


def _t_github_pr(args: dict, cwd: str) -> dict:
    """Open a GitHub pull request from the current branch via the ``gh`` CLI.
    Args: title (req), body, base (default 'main'), head (default current
    branch), draft. Requires gh installed + authenticated in the repo."""
    if not args.get("title"):
        return {"ok": False, "error": "missing 'title'"}
    import shutil
    if not shutil.which("gh"):
        return {"ok": False, "error": "gh_not_installed",
                "hint": "install the GitHub CLI (gh) + `gh auth login`"}
    cmd = ["gh", "pr", "create", "--title", str(args["title"]),
           "--body", str(args.get("body") or "")]
    cmd += ["--base", str(args.get("base") or "main")]
    if args.get("head"):
        cmd += ["--head", str(args["head"])]
    if args.get("draft"):
        cmd += ["--draft"]
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr or out or "gh failed").strip()[:800]}
    return {"ok": True, "url": out, "written": {"title": args.get("title"),
            "base": args.get("base") or "main"}}
