"""Old, large read output is saved and shrunk; recent output, errors and edits are not."""
import pytest

from aiforge_core.runtime import context_offload as co
from aiforge_core.runtime.chat_agent._context import _aging as A


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_CHAT_AGE_OBS", raising=False)
    monkeypatch.setenv("AIFORGE_CHAT_AGE_BURST", "1")


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
    assert A.age_observations(_convo(tool="file_write")) == 0       # an edit result stays
    assert A.age_observations(_convo(body="y" * 500)) == 0          # small


def test_it_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_AGE_OBS", "0")
    assert A.age_observations(_convo()) == 0


def test_command_output_ages_but_keeps_its_error_lines():
    body = "ok line\n" * 600 + "FAILED tests/test_a.py::t\nE   AssertionError: 1 != 2\n" + "tail\n" * 50
    convo = _convo(tool="run_command", body=body)
    assert A.age_observations(convo) == 1
    text = convo[2]["content"]
    assert "FAILED tests/test_a.py::t" in text and "AssertionError: 1 != 2" in text
    assert len(text) < 2500


def test_aging_waits_for_a_burst(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_AGE_BURST", "3")
    convo = _convo()                       # one old read only
    assert A.age_observations(convo) == 0 and "[aged:" not in convo[2]["content"]
    for _ in range(2):
        convo[1:1] = _pair("file_read", "y" * 6000)
    assert A.age_observations(convo) == 3
