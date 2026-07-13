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
