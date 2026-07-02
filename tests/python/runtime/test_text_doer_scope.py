"""Fix 3 — scope_allowlist_globs must be enforced on the LOCAL text-doer path.

The native Doer had a scope_guard before_tool_callback; a FunctionNode can't
carry it, so the production text path only had the worktree jail and a local
model could edit ANY file in the worktree. This drives run_text_doer with a
scripted complete_fn that emits file_write ACTIONs and asserts an in-scope
write lands while an out-of-scope write is refused (no file written).
Backward compatible: with globs unset, both writes land."""
from __future__ import annotations

from aiforge_core.runtime import text_doer as td


def _scripted(outputs):
    seq = list(outputs)

    def _fn(role, messages, **kw):
        return seq.pop(0)

    return _fn


def test_out_of_scope_write_rejected(tmp_path):
    fn = _scripted([
        'ACTION: file_write\n'
        'ARGS_JSON: {"path": "src/app.py", "content": "x = 1\\n"}',
        'ACTION: file_write\n'
        'ARGS_JSON: {"path": "secrets.env", "content": "TOKEN=abc\\n"}',
        "FINAL: done",
    ])
    out = td.run_text_doer(
        {"plan_md": "p", "scope_allowlist_globs": ["src/**"]},
        str(tmp_path), complete_fn=fn)
    # in-scope write landed
    assert (tmp_path / "src" / "app.py").read_text() == "x = 1\n"
    # out-of-scope write refused — no file on disk
    assert not (tmp_path / "secrets.env").exists()
    assert out["doer_outcome"]  # still produced an outcome, never crashed


def test_globs_unset_allows_everything(tmp_path):
    fn = _scripted([
        'ACTION: file_write\n'
        'ARGS_JSON: {"path": "src/app.py", "content": "x = 1\\n"}',
        'ACTION: file_write\n'
        'ARGS_JSON: {"path": "secrets.env", "content": "TOKEN=abc\\n"}',
        "FINAL: done",
    ])
    td.run_text_doer({"plan_md": "p"}, str(tmp_path), complete_fn=fn)
    assert (tmp_path / "src" / "app.py").read_text() == "x = 1\n"
    assert (tmp_path / "secrets.env").read_text() == "TOKEN=abc\n"


def test_scope_globs_csv_string_normalised(tmp_path):
    # scope_allowlist_globs may arrive as a comma-joined string.
    fn = _scripted([
        'ACTION: file_write\n'
        'ARGS_JSON: {"path": "docs/readme.md", "content": "hi\\n"}',
        "FINAL: done",
    ])
    td.run_text_doer(
        {"plan_md": "p", "scope_allowlist_globs": "src/**"},
        str(tmp_path), complete_fn=fn)
    assert not (tmp_path / "docs" / "readme.md").exists()
