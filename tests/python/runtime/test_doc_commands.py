"""Post-edit check on docs: a run command for the project's own name or
module that does not exist here (README said `invtool --low 10 x.csv`)."""
from __future__ import annotations

import textwrap

from aiforge_core.runtime import post_edit as P


def _repo(tmp_path, readme, extra=None):
    (tmp_path / ".git").mkdir()
    pkg = tmp_path / "invtool"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "cli.py").write_text("def main(): pass\n")
    for name, text in (extra or {}).items():
        (tmp_path / name).write_text(text)
    f = tmp_path / "README.md"
    f.write_text(textwrap.dedent(readme))
    return str(f)


def test_the_undeclared_project_command_is_reported(tmp_path):
    f = _repo(tmp_path, """
        ## Usage
        ```bash
        invtool --low 10 inventory.csv
        ```
    """)
    out = P._doc_errors(f, str(tmp_path))
    assert len(out) == 1 and "declares no `invtool` command" in out[0] and ":4:" in out[0]


def test_a_declared_console_script_or_real_module_passes(tmp_path):
    f = _repo(tmp_path, """
        ```bash
        invtool data.csv
        python -m invtool.cli data.csv --low 5
        $ python3 -m json.tool x.json
        ```
    """, {"pyproject.toml": '[project]\nname = "invtool"\n[project.scripts]\ninvtool = "invtool.cli:main"\n'})
    assert P._doc_errors(f, str(tmp_path)) == []


def test_a_missing_module_is_reported(tmp_path):
    f = _repo(tmp_path, "```sh\npython -m invtool.main data.csv\n```\n")
    out = P._doc_errors(f, str(tmp_path))
    assert len(out) == 1 and "no module `invtool.main`" in out[0]


def test_other_tools_and_prose_are_never_judged(tmp_path):
    f = _repo(tmp_path, """
        Run invtool on a CSV (prose, not a command).
        ```bash
        docker run --rm img
        kubectl apply -f k8s.yaml
        pip install -e . && npm run build
        cd invtool && ls
        ```
        ```python
        invtool = load()
        ```
    """)
    assert P._doc_errors(f, str(tmp_path)) == []


def test_package_json_bin_counts(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "package.json").write_text('{"name": "mytool", "bin": {"mytool": "cli.js"}}')
    f = tmp_path / "README.md"
    f.write_text("```bash\nmytool --help\n```\n")
    assert P._doc_errors(str(f), str(tmp_path)) == []


def test_python_m_of_other_modules_is_not_judged(tmp_path):
    f = _repo(tmp_path, "```bash\npython -m pip install invtool\npython -m rick_portal_gun\n```\n")
    assert P._doc_errors(f, str(tmp_path)) == []
