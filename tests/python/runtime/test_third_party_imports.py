"""_third_party_imports must exclude ALL stdlib (a stray stdlib name like
`secrets` in the venv pip-install list made the whole install fail → pytest
missing → test gate blind). Use the authoritative stdlib set."""
import os


def test_excludes_stdlib_includes_real_third_party(tmp_path):
    from aiforge_core.runtime.integration_report import _third_party_imports
    (tmp_path / "auth.py").write_text(
        "import secrets\nimport hashlib\nimport base64\nimport hmac\n"
        "import sqlite3\nfrom flask import Flask\nimport requests\n")
    mods = set(_third_party_imports(str(tmp_path)))
    # real third-party present
    assert "flask" in mods
    assert "requests" in mods
    # stdlib must NOT leak into the pip-install list
    for std in ("secrets", "hashlib", "base64", "hmac", "sqlite3"):
        assert std not in mods, f"{std} wrongly flagged third-party: {mods}"


def test_local_modules_excluded(tmp_path):
    from aiforge_core.runtime.integration_report import _third_party_imports
    (tmp_path / "store.py").write_text("x = 1\n")
    (tmp_path / "app.py").write_text("import store\nfrom flask import Flask\n")
    mods = set(_third_party_imports(str(tmp_path)))
    assert "store" not in mods
    assert "flask" in mods
