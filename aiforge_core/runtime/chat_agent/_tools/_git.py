from __future__ import annotations

from ._shared import _coerce_int, _git_cli


def _t_git_status(args: dict, cwd: str) -> dict:
    return _git_cli(["status", "--porcelain=v1", "-b"], cwd)


def _t_git_diff(args: dict, cwd: str) -> dict:
    argv = ["--no-pager", "diff"] + (["--staged"] if args.get("staged") else [])
    if args.get("path"):
        argv += ["--", str(args["path"])]
    return _git_cli(argv, cwd)


def _t_git_log(args: dict, cwd: str) -> dict:
    n = max(1, min(_coerce_int(args.get("limit"), 20) or 20, 200))
    argv = ["--no-pager", "log", f"-{n}", "--oneline", "--decorate"]
    if args.get("path"):
        argv += ["--", str(args["path"])]
    return _git_cli(argv, cwd)


def _t_git_blame(args: dict, cwd: str) -> dict:
    argv = ["--no-pager", "blame", "--date=short"]
    _s, _e = _coerce_int(args.get("start")), _coerce_int(args.get("end"))
    if _s and _e:
        argv += ["-L", f"{_s},{_e}"]
    argv += ["--", str(args.get("path") or "")]
    return _git_cli(argv, cwd)
