from __future__ import annotations

from pathlib import Path

from .._shell import _resolve, _syntax_check
from ._shared import _coerce_int, _elaborate_body


def _t_skill_search(args: dict, cwd: str) -> dict:
    """Search the skill registry (SKILL.md playbooks) by relevance."""
    try:
        from aiforge_core.runtime import skills as _skills
        q = args.get("query") or args.get("q") or ""
        hits = _skills.search(q, cwd, k=int(args.get("k", 5)))
        return {"ok": True, "skills": hits}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_learn_skill(args: dict, cwd: str) -> dict:
    """Author a reusable skill (SKILL.md) so future sessions reuse the
    solution. scope: 'global' (all repos) or 'repo' (this repo)."""
    try:
        from aiforge_core.runtime import skills as _skills
        triggers = args.get("triggers") or []
        if isinstance(triggers, str):
            triggers = [t.strip() for t in triggers.split(",") if t.strip()]
        _name = args.get("name", "")
        _desc = args.get("description", "")
        _body = _elaborate_body("skill", args.get("body") or args.get("content")
                                or "", name=_name, description=_desc)
        return _skills.write_skill(
            name=_name, description=_desc, body=_body,
            triggers=list(triggers), cwd=cwd,
            scope=(args.get("scope") or "global").lower(),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_workflow_search(args: dict, cwd: str) -> dict:
    """Search the workflow registry (WORKFLOW.md procedures) by relevance."""
    try:
        from aiforge_core.runtime import workflows as _wf
        q = args.get("query") or args.get("q") or ""
        return {"ok": True, "workflows": _wf.search(q, cwd, k=int(args.get("k", 5)))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_learn_workflow(args: dict, cwd: str) -> dict:
    """Author a reusable workflow (WORKFLOW.md) — an end-to-end procedure —
    so future sessions (or the user) can reuse it. scope: 'global' or 'repo'.
    Optional ``scripts`` land in the workflow's own ``scripts/`` folder;
    write_workflow HARD-tests each one (syntax check + actually RUNS its
    ``test`` command or the script itself) and REFUSES the save on any
    failure — job-builder parity, no honour-system flag."""
    try:
        from aiforge_core.runtime import workflows as _wf
        triggers = args.get("triggers") or []
        if isinstance(triggers, str):
            triggers = [t.strip() for t in triggers.split(",") if t.strip()]
        scripts = args.get("scripts") or []
        _name = args.get("name", "")
        _desc = args.get("description", "")
        _body = _elaborate_body("workflow", args.get("body") or args.get("content")
                                or "", name=_name, description=_desc)
        return _wf.write_workflow(
            name=_name, description=_desc, body=_body,
            triggers=list(triggers), cwd=cwd,
            scope=(args.get("scope") or "global").lower(),
            scripts=scripts,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_editor(args: dict, _cwd: str) -> dict:
    from aiforge_core.runtime.tools.editor import editor
    vr = args.get("view_range")
    if isinstance(vr, list):
        vr = [_coerce_int(x) for x in vr]
        if any(x is None for x in vr):
            vr = None
    return editor(
        command=str(args.get("command") or args.get("sub_command") or "view"),
        path=str(args.get("path") or ""),
        file_text=args.get("file_text") if args.get("file_text") is not None else args.get("content"),
        old_str=args.get("old_str") if args.get("old_str") is not None else args.get("old_text"),
        new_str=args.get("new_str") if args.get("new_str") is not None else args.get("new_text"),
        insert_line=_coerce_int(args.get("insert_line")),
        view_range=vr,
    )


class _EditBatch:
    """The working state of a multi-edit: chained content per file, plus the
    pre-edit content each file needs for rollback."""

    __slots__ = ("pending", "original", "rel_of")

    def __init__(self) -> None:
        self.pending: dict[str, str] = {}   # abs path -> working content
        self.original: dict[str, str] = {}  # abs path -> pre-edit disk content
        self.rel_of: dict[str, str] = {}    # abs path -> the path the model gave

    def load(self, ap: str, rel: str) -> bool:
        """Read the file once; later edits chain onto the working copy."""
        self.rel_of.setdefault(ap, rel)
        if ap in self.pending:
            return True
        try:
            self.pending[ap] = Path(ap).read_text(encoding="utf-8",
                                                  errors="replace")
        except FileNotFoundError:
            return False
        self.original[ap] = self.pending[ap]
        return True


def _edit_fields(e: dict) -> tuple[str, object, object]:
    """``(path, old, new)`` — both spellings of the two text fields."""
    old = e.get("old_str") if e.get("old_str") is not None else e.get("old_text")
    new = e.get("new_str") if e.get("new_str") is not None else e.get("new_text")
    return str(e.get("path") or "").strip(), old, new


def _stage_edit(i: int, e: dict, batch: _EditBatch, cwd: str) -> dict | None:
    """Validate + apply ONE edit to the working copy. Returns an error dict."""
    if not isinstance(e, dict):
        return {"ok": False, "error": f"edit #{i} is not an object"}
    path, old, new = _edit_fields(e)
    if not path or old is None or new is None:
        return {"ok": False, "error": f"edit #{i} needs path + old_str + new_str"}
    if old == "":
        return {"ok": False, "error": f"edit #{i}: old_str must be non-empty"}
    try:
        ap = str(_resolve(cwd, path))
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}
    if not batch.load(ap, path):
        return {"ok": False, "error": f"edit #{i}: file not found: {path}"}
    body = batch.pending[ap]
    cnt = body.count(old)
    if cnt == 0:
        return {"ok": False, "error": f"edit #{i}: old_str not found in {path}"}
    if cnt > 1 and not e.get("replace_all"):
        return {"ok": False, "error": f"edit #{i}: old_str appears {cnt}× in "
                f"{path} — pass replace_all:true or make it unique"}
    batch.pending[ap] = (body.replace(old, new) if e.get("replace_all")
                         else body.replace(old, new, 1))
    return None


def _write_batch(batch: _EditBatch) -> tuple[list, str]:
    """Write every file; on ANY failure roll every written file back.
    Returns ``(written_rel_paths, error)``."""
    written: list[str] = []
    done: list[str] = []
    try:
        for ap, content in batch.pending.items():
            Path(ap).write_text(content, encoding="utf-8")
            done.append(ap)
            written.append(batch.rel_of.get(ap, ap))
    except Exception as exc:  # noqa: BLE001 — restore the pre-edit state
        for ap in done:
            try:
                Path(ap).write_text(batch.original[ap], encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        return [], f"write failed, rolled back: {exc}"
    return written, ""


def _t_multi_edit(args: dict, cwd: str) -> dict:
    """Apply a BATCH of find/replace edits across one or more files in a single
    call — validated first, then applied atomically (snapshot + rollback). Each
    edit: ``{"path","old_str","new_str","replace_all"?}``."""
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits:
        return {"ok": False, "error": "edits must be a non-empty list of "
                "{path, old_str, new_str, replace_all?}"}
    batch = _EditBatch()
    for i, e in enumerate(edits):
        err = _stage_edit(i, e, batch, cwd)
        if err is not None:
            return err
    # Syntax-guard each resulting code file (skipped for non-code / force:true).
    for ap, content in batch.pending.items():
        bad = _syntax_check(ap, content, args)
        if bad:
            return {"ok": False, "error": "syntax_invalid",
                    "file": batch.rel_of.get(ap, ap), "detail": bad,
                    "hint": "fix the edit or pass force:true"}
    written, err = _write_batch(batch)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "files": written, "edits_applied": len(edits)}


def _t_typecheck(args: dict, _cwd: str) -> dict:
    from aiforge_core.runtime.tools.typecheck import typecheck
    return typecheck()


def _t_format(args: dict, _cwd: str) -> dict:
    from aiforge_core.runtime.tools.format import format as _fmt
    return _fmt(str(args.get("path") or "."))


def _t_lsp(args: dict, _cwd: str) -> dict:
    from aiforge_core.runtime.tools.lsp import lsp
    return lsp(command=str(args.get("command") or ""), path=str(args.get("path") or ""),
               line=_coerce_int(args.get("line"), 0), character=_coerce_int(args.get("character"), 0))


def _t_run_tests(args: dict, _cwd: str) -> dict:
    from aiforge_core.runtime.tools.test_runner import run_tests
    return run_tests(mode=str(args.get("mode") or "fast"), pattern=str(args.get("pattern") or ""))
