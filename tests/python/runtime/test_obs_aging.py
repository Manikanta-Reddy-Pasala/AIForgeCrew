"""Old, large read output is saved and shrunk; recent output, errors and edits are not."""
import pytest

from aiforge_core.runtime import context_offload as co
from aiforge_core.runtime.chat_agent._context import _aging as A


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_CHAT_AGE_OBS", raising=False)


def _pair(tool, body):
    return [{"role": "assistant", "content": f"ACTION: {tool}\nARGS_JSON: {{}}"},
            {"role": "user", "content": "OBSERVATION: " + body}]


def _convo(tool="file_read", body="x" * 6000, tail=12):
    msgs = [{"role": "system", "content": "s"}] + _pair(tool, body)
    for _ in range(tail // 2):
        msgs += _pair("run_command", "ok")
    return msgs


def test_an_old_large_read_is_saved_and_shrunk_in_place():
    convo = _convo()
    assert A.age_observations(convo) == 1
    text = convo[2]["content"]
    assert text.startswith("OBSERVATION: [aged: file_read output, 6000 chars")
    assert len(text) < 1500
    oid = text.split('"id": "')[1].split('"')[0]
    assert co.load(oid)["text"].endswith("x" * 6000)      # nothing lost
    assert A.age_observations(convo) == 0                 # idempotent


def test_recent_commands_and_small_output_are_left_alone():
    assert A.age_observations(_convo(tail=4)) == 0                  # too recent
    assert A.age_observations(_convo(tool="run_command")) == 0      # not a read
    assert A.age_observations(_convo(body="y" * 500)) == 0          # small


def test_it_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_AGE_OBS", "0")
    assert A.age_observations(_convo()) == 0
