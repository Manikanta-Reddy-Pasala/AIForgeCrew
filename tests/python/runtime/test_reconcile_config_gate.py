"""Reconcile config-validity gate — live-e2e finding: a broken pyproject.toml
blinded all reconcile passes ('failed (0 failing)')."""
from aiforge_core.runtime import parallel_subtasks as pp


def test_broken_pyproject_detected(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-v --cov=.\n')  # unterminated
    err = pp._broken_project_config(str(tmp_path))
    assert err and err.startswith("pyproject.toml:")


def test_valid_configs_pass(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n')
    (tmp_path / "package.json").write_text('{"name": "x"}')
    assert pp._broken_project_config(str(tmp_path)) is None


def test_broken_package_json_detected(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "x",}')
    err = pp._broken_project_config(str(tmp_path))
    assert err and err.startswith("package.json:")
