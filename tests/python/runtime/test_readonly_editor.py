"""The gatherers' ``editor`` is view-only, as agents.yaml declares.

Live: the request's "make the change" directive reaches every stage, and a
Researcher / context gatherer holding the full editor made the edit itself —
before the Planner and the Doer ran, outside the Doer's guards."""
import pytest

pytest.importorskip("google.adk.tools")

from aiforge_core.runtime import doer_tools  # noqa: E402


def _editor(role):
    tools = {(getattr(t, "name", None) or t.func.__name__): t
             for t in doer_tools.adk_function_tools(role=role)}
    return tools["editor"].func


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AIFORGE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("AIFORGE_TOOL_ENFORCE", raising=False)
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    return tmp_path


@pytest.mark.parametrize("role", ["researcher", "ctx_repomap", "ctx_conventions"])
def test_a_gatherer_can_view_but_not_edit(repo, role):
    editor = _editor(role)
    seen = editor("view", "calc.py")
    assert seen["ok"] and "def add" in str(seen)
    for call in ({"command": "str_replace", "path": "calc.py",
                  "old_str": "a + b", "new_str": "a - b"},
                 {"command": "create", "path": "new.py", "file_text": "x = 1\n"},
                 {"command": "insert", "path": "calc.py", "insert_line": 0,
                  "new_str": "# hi\n"}):
        out = editor(**call)
        assert out["ok"] is False
        assert out["error"] == "editor_command_not_allowed", call
    assert (repo / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
    assert not (repo / "new.py").exists()


def test_the_role_is_not_an_argument_the_model_can_set(repo):
    import inspect
    assert "_agent_role" not in inspect.signature(_editor("researcher")).parameters


def test_the_doer_keeps_the_full_editor(repo):
    out = _editor("doer")("create", "new.py", file_text="x = 1\n")
    assert out["ok"] and (repo / "new.py").read_text() == "x = 1\n"
