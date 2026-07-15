"""list_flags() prunes session-scoped gate flags whose session no longer
exists — so a stale 'commits auto-approved · session 7020' from a deleted
session self-cleans out of the Auto-approvals panel."""
from __future__ import annotations

import json

from aiforge_core.runtime import rule_capture


def _setup(monkeypatch, tmp_path, live_ids):
    monkeypatch.setattr(rule_capture, "_flags_path", lambda: tmp_path / "rule_flags.json")
    monkeypatch.setattr(
        "aiforge_core.runtime.chat_store.list_sessions",
        lambda: [{"id": i} for i in live_ids])


def test_prunes_flag_for_deleted_session(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, live_ids=[30, 29])
    (tmp_path / "rule_flags.json").write_text(json.dumps({
        "global": {}, "repo": {},
        "session": {"7020": {"commit_auto_approve": True},
                    "30": {"commit_auto_approve": True}}}), encoding="utf-8")
    out = rule_capture.list_flags()
    assert "7020" not in out["session"]           # deleted session → gone
    assert out["session"].get("30") == {"commit_auto_approve": True}  # live kept
    # persisted: the file no longer carries the stale entry
    on_disk = json.loads((tmp_path / "rule_flags.json").read_text())
    assert "7020" not in on_disk.get("session", {})


def test_keeps_flags_when_liveness_unknown(monkeypatch, tmp_path):
    # chat_store raising → don't prune (never drop a possibly-live flag)
    monkeypatch.setattr(rule_capture, "_flags_path", lambda: tmp_path / "rule_flags.json")
    monkeypatch.setattr("aiforge_core.runtime.chat_store.list_sessions",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    (tmp_path / "rule_flags.json").write_text(json.dumps({
        "global": {}, "repo": {},
        "session": {"7020": {"commit_auto_approve": True}}}), encoding="utf-8")
    out = rule_capture.list_flags()
    assert out["session"].get("7020") == {"commit_auto_approve": True}
