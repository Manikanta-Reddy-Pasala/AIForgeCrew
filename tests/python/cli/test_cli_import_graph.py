"""The CLI must stay freezable.

`aiforge_cli` is compiled into a ~15 MB single-file binary for macOS, Linux and
Windows. One import of aiforge_core would drag ADK, litellm, scipy and
tree-sitter into that binary — hundreds of megabytes that cannot be frozen and
are not needed on the host, because the engine runs in the sandbox. This test
is the guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "packages" / "aiforge_cli" / "aiforge_cli"
ALLOWED_THIRD_PARTY = {"httpx", "prompt_toolkit"}
# _entry.py imports `aiforge_cli.cli` absolutely: PyInstaller runs the frozen
# entry script as top-level __main__, where a relative import has no parent.
OWN = {"aiforge_cli"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_no_module_imports_the_engine():
    offenders = {p.name: sorted(i for i in _imports(p) if i == "aiforge_core")
                 for p in SRC.glob("*.py")}
    assert not any(offenders.values()), f"aiforge_core leaked into the CLI: {offenders}"


def test_the_dependency_set_is_the_declared_one():
    stdlib = set(__import__("sys").stdlib_module_names)
    third_party: set[str] = set()
    for path in SRC.glob("*.py"):
        third_party |= {i for i in _imports(path)
                        if i not in stdlib and not i.startswith("_")}
    third_party -= OWN
    assert third_party <= ALLOWED_THIRD_PARTY, (
        f"undeclared dependency in the CLI: {sorted(third_party - ALLOWED_THIRD_PARTY)}")
