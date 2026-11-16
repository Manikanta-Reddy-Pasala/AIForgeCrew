"""Per-host workflows load from ``$AIFORGE_CONFIG_DIR/workflows/*.py``."""
from __future__ import annotations

import sys

from aiforge_core.workflows import _user, registry as R

_PLUGIN = '''
from aiforge_core.workflows.registry import WorkflowSpec, register

def run_workflow(ticket, *, log=None):
    return {"udiff": "# report for " + ticket["identifier"], "problems": [],
            "blocked_by_detectors": False}

register(WorkflowSpec(id="host-recon", label="Host reconciliation",
                      handler=f"{__name__}:run_workflow",
                      triggers={"keywords_any": ["host recon"]}))
'''


def test_user_workflow_registers_and_dispatches(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "REGISTRY", {})
    folder = tmp_path / "workflows"
    folder.mkdir()
    (folder / "host_recon.py").write_text(_PLUGIN, encoding="utf-8")
    (folder / "broken.py").write_text("raise RuntimeError('bad plugin')\n", encoding="utf-8")
    monkeypatch.delitem(sys.modules, f"{_user.PACKAGE}.host_recon", raising=False)
    try:
        loaded = _user.load_user_workflows(folder)
        assert loaded == [f"{_user.PACKAGE}.host_recon"]       # broken one skipped
        assert R.get("host-recon").handler == f"{_user.PACKAGE}.host_recon:run_workflow"
        out = R.dispatch("host-recon", {"identifier": "JIRA-7"})
        assert out["udiff"] == "# report for JIRA-7"
    finally:
        sys.modules.pop(f"{_user.PACKAGE}.host_recon", None)


def test_missing_user_folder_loads_nothing(tmp_path):
    assert _user.load_user_workflows(tmp_path / "nope") == []


def test_default_folder_follows_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    assert _user.user_workflow_dir() == tmp_path / "workflows"


def test_attachment_roles_are_configured_not_built_in(monkeypatch):
    from aiforge_core.memory import online_learner as ol

    monkeypatch.delenv("AIFORGE_ATTACHMENT_ROLES", raising=False)
    assert ol.detect_attachment_role("ledger-acme.xlsx") == "other"
    monkeypatch.setenv("AIFORGE_ATTACHMENT_ROLES", "ledger, export")
    assert ol.detect_attachment_role("Ledger-Acme.xlsx") == "ledger"
    assert ol.detect_attachment_role("acme-export.csv") == "export"
    assert ol.detect_attachment_role("shot.png") == "screenshot"
