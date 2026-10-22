# tests/python/runtime/test_target_reask_guard.py
import pytest

from aiforge_core.runtime import targets as T
from aiforge_core.runtime.chat_agent._guards import base
from aiforge_core.runtime.chat_agent._guards import targets as tg

JSON = '{"a": 1}' + " " * 200


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_TARGETS", "1")
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))


class St:
    session_id = 5
    role = "chat"
    def __init__(self):
        self.convo = []


def test_guard_sends_back_once_then_labels():
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    st = St()
    g = tg.TargetReaskGuard()
    step = {"text": "Could you paste the expected output again?"}
    list(base._check(g, st, step))
    assert st.convo and "target-1" in st.convo[-1]["content"] and ".json" in st.convo[-1]["content"]
    step2 = {"text": "Please share the expected JSON."}
    list(base._check(g, st, step2))
    assert step2["text"].startswith("⚠")


def test_guard_is_silent_without_targets_or_without_an_ask():
    st = St()
    g = tg.TargetReaskGuard()
    assert g.applies(st) is False
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    assert g.applies(st) is True and g.detect("Done, diff is empty.") == []
