"""Per-chat-mode approval toggle — store + gate helper behaviour."""
import importlib


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    import aiforge_core.config.approval_settings as a
    return importlib.reload(a)


def test_default_all_on(tmp_path, monkeypatch):
    a = _fresh(tmp_path, monkeypatch)
    assert a.required("chat") is True
    assert a.required("plan") is True
    assert a.required("pipeline") is True
    assert a.all_modes() == {"simple": True, "plan": True, "team": True}


def test_ui_aliases_resolve(tmp_path, monkeypatch):
    a = _fresh(tmp_path, monkeypatch)
    a.set_mode("chat", False)
    a.set_mode("pipeline", False)
    assert a.required("chat") is False
    assert a.required("simple") is False     # canonical
    assert a.required("pipeline") is False
    assert a.required("team") is False
    assert a.required("plan") is True        # untouched


def test_persists(tmp_path, monkeypatch):
    a = _fresh(tmp_path, monkeypatch)
    a.set_mode("plan", False)
    a2 = _fresh(tmp_path, monkeypatch)
    assert a2.required("plan") is False


def test_unknown_mode_fails_safe(tmp_path, monkeypatch):
    a = _fresh(tmp_path, monkeypatch)
    assert a.required("bogus") is True       # default ON
    import pytest
    with pytest.raises(ValueError):
        a.set_mode("bogus", False)


def test_chat_approve_helper_reads_setting(tmp_path, monkeypatch):
    a = _fresh(tmp_path, monkeypatch)
    a.set_mode("plan", False)
    import aiforge_core.runtime.chat_approve as ca
    importlib.reload(ca)
    ca.set_mode(4242, "plan")
    assert ca.approvals_required(4242) is False
    ca.set_mode(4242, "simple")
    assert ca.approvals_required(4242) is True   # simple still ON
    ca.finish(4242)
    # After finish, no mode → fails safe ON.
    assert ca.approvals_required(4242) is True


def test_one_chat_toggle_leaves_other_chats_alone(tmp_path, monkeypatch):
    a = _fresh(tmp_path, monkeypatch)
    a.set_session(1, "chat", False)
    assert a.required("chat", 1) is False
    assert a.required("chat", 2) is True      # another chat keeps the default
    assert a.required("chat") is True         # the Settings default untouched
    assert a.required("plan", 1) is True      # only that mode in that chat


def test_chat_choice_wins_over_default_both_ways(tmp_path, monkeypatch):
    a = _fresh(tmp_path, monkeypatch)
    a.set_mode("team", False)
    a.set_session(7, "pipeline", True)
    assert a.required("team", 7) is True
    assert a.required("team", 8) is False
    assert a.session_modes(7) == {"simple": True, "plan": True, "team": True}


def test_chat_choice_persists_and_is_forgotten(tmp_path, monkeypatch):
    a = _fresh(tmp_path, monkeypatch)
    a.set_session(3, "plan", False)
    a.set_session(4, "plan", False)
    a2 = _fresh(tmp_path, monkeypatch)
    assert a2.required("plan", 3) is False
    a2.forget_session(3)
    assert a2.required("plan", 3) is True
    assert a2.required("plan", 4) is False
    a2.forget_session()                       # delete-all: ids restart at 1
    assert a2.required("plan", 4) is True


def test_chat_choice_unknown_mode_and_corrupt_file(tmp_path, monkeypatch):
    a = _fresh(tmp_path, monkeypatch)
    import pytest
    with pytest.raises(ValueError):
        a.set_session(1, "bogus", False)
    a.set_mode("chat", False)
    (tmp_path / "chat-approvals.json").write_text("{not json")
    assert a.required("chat", 1) is True      # corrupt → ON, not the "off" default
    assert a.required("chat") is False        # no chat → the default as before
    with pytest.raises(OSError):              # never start over: another chat's
        a.set_session(1, "chat", False)       # "on" would be lost
    assert a.required("chat", 1) is True
    a.forget_session(1)                       # no crash, nothing written
    a.copy_session(1, 2)
    assert (tmp_path / "chat-approvals.json").read_text() == "{not json"


def test_side_task_keeps_parent_choice(tmp_path, monkeypatch):
    a = _fresh(tmp_path, monkeypatch)
    a.set_mode("chat", False)
    a.set_session(10, "chat", True)
    a.copy_session(10, 11)
    assert a.required("chat", 11) is True
    a.copy_session(12, 13)                    # parent made no choice → no-op
    assert a.required("chat", 13) is False


def test_chat_approve_helper_reads_chat_choice(tmp_path, monkeypatch):
    a = _fresh(tmp_path, monkeypatch)
    import aiforge_core.runtime.chat_approve as ca
    importlib.reload(ca)
    a.set_session(51, "chat", False)
    ca.set_mode(51, "simple")
    ca.set_mode(52, "simple")
    assert ca.approvals_required(51) is False
    assert ca.approvals_required(52) is True
    ca.finish(51)
    ca.finish(52)


def test_chat_store_delete_paths_drop_chat_choices(tmp_path, monkeypatch):
    # Every delete goes through chat_store (the reset route, memory admin's
    # clear): a NEW chat 1 must not inherit an old chat 1's "off".
    a = _fresh(tmp_path, monkeypatch)
    from aiforge_core.runtime.chat_store import _api
    a.set_session(1, "chat", False)
    a.set_session(2, "chat", False)
    _api._forget_grants(1)
    assert a.required("chat", 1) is True
    assert a.required("chat", 2) is False
    _api._forget_grants()
    assert a.required("chat", 2) is True
