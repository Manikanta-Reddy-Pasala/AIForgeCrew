from __future__ import annotations

import os


def _fire_stop(reason: str, cwd: str) -> None:
    """Best-effort Stop lifecycle hook at a terminal loop exit. Soft-fail: a
    hooks error must never break the turn's clean shutdown."""
    try:
        from aiforge_core.runtime import hooks as _hooks
        _hooks.fire("Stop", {"reason": reason}, cwd)
    except Exception:  # noqa: BLE001
        pass


_EDIT_TOOL_NAMES = frozenset((
    "write_file", "file_write", "edit", "editor", "edit_block", "file_patch",
    "patch", "apply_patch", "str_replace", "create_file",
    # These land real edits too but were missing — so genuine edits via them
    # didn't set _edits_made (verify gate skipped) and could trip the
    # claim-vs-reality guard on a truthful "I edited …" final.
    "multi_edit", "file_create",
))


def _verify_on_final_enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_VERIFY_ON_FINAL", "1") not in ("0", "false")


def _verify_max_rounds() -> int:
    try:
        return max(0, min(12, int(os.environ.get("AIFORGE_CHAT_VERIFY_ROUNDS", "6"))))
    except ValueError:
        return 6


def _run_project_verify(cwd: str):
    """Run the project's test/build gate on ``cwd``. Returns ``(ok, output)`` or
    ``(None, "")`` when there's nothing to test — reuses the SAME runner the
    pipeline reconcile uses, so simple/doer runs get the same real pass/fail."""
    try:
        from aiforge_core.runtime.parallel_subtasks import _project_test_output
        return _project_test_output(cwd)
    except Exception:  # noqa: BLE001
        return None, ""


def _post_edit_syntax_error(_name: str, args: dict, cwd: str) -> "str | None":
    """Syntax-check the file an edit tool just wrote. Returns an error string
    when broken, else None. Reuses the pipeline's language-agnostic syntax_guard
    (Python compile, js/java/go/… checkers, brace-balance fallback)."""
    path = None
    for k in ("path", "file", "filename", "file_path", "target"):
        v = (args or {}).get(k)
        if isinstance(v, str) and v:
            path = v
            break
    if not path:
        return None
    abs_path = path if os.path.isabs(path) else os.path.join(cwd, path)
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except Exception:  # noqa: BLE001
        return None
    try:
        from aiforge_core.runtime.syntax_guard import validate_syntax
        ok, err = validate_syntax(path, content)
        return None if ok else err
    except Exception:  # noqa: BLE001
        return None


def _verify_fix_message(output: str) -> str:
    """A directed 'tests are failing, fix them' turn — reuses the reconcile's
    root-cause hint engine so the local model gets concrete guidance, not just
    the raw traceback."""
    try:
        from aiforge_core.runtime.parallel_subtasks import _directed_hints
        hints = _directed_hints(output or "")
    except Exception:  # noqa: BLE001
        hints = []
    hint_block = ("\n\nDIRECTED FIXES:\n" + "\n".join(f"- {h}" for h in hints)) if hints else ""
    return (
        "[automated verification — not the user] You said you were done, but the "
        "project's tests do NOT pass. Do NOT reply FINAL until they do. Read the "
        "failures below, fix the IMPLEMENTATION (not the tests, unless a test "
        "clearly contradicts the request), then re-run the tests to confirm.\n\n"
        "TEST OUTPUT:\n```\n" + (output or "")[-3000:] + "\n```" + hint_block)
