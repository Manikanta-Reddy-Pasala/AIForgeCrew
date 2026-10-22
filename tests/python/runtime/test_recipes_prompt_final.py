import types
import pytest
from aiforge_core.runtime import recipes as R
from aiforge_core.runtime.chat_agent._turn import _blocks, _finish


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_RECIPES", "1")


def test_recipes_block_sits_in_the_priority_tier(tmp_path):
    R.save(str(tmp_path), "test", "./t.sh")
    out = _blocks._prepend_priority_blocks("CORE", [], "", "RULES x", False, False, "",
                                           recipes_text=R.block(str(tmp_path)))
    assert out.index("RULES x") < out.index("RECIPES") < out.index("CORE")


def test_final_emits_recipe_offers_after_done(tmp_path, monkeypatch):
    from aiforge_core.runtime import action_log
    steps = [{"type": "tool", "name": "run_command", "args": {"cmd": "pytest -q"},
              "result": {"ok": True, "code": 0}}]
    monkeypatch.setattr(action_log, "live_steps", lambda sid: steps)
    st = types.SimpleNamespace(session_id=7, cwd=str(tmp_path))
    evs = list(_finish._recipe_offer_events(st, str(tmp_path)))
    assert evs == [{"type": "recipe_offer", "verb": "test", "cmd": "pytest -q", "cwd": "."}]
    R.save(str(tmp_path), "test", "pytest -q")
    assert list(_finish._recipe_offer_events(st, str(tmp_path))) == []


def test_recipe_offer_event_is_persisted_as_a_step():
    from aiforge_core.api.routes._chat import _turn_events
    ev = {"type": "recipe_offer", "verb": "test", "cmd": "pytest -q", "cwd": "."}
    steps: list = []
    _turn_events._route_produce_event(ev, {}, steps)
    assert steps == [ev]
