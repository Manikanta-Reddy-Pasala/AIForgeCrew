"""A condense keeps the paths and errors of the dropped middle, verbatim."""
from aiforge_core.runtime.chat_agent._context import _compaction as C


def _call(tool, **args):
    import json
    return {"role": "assistant",
            "content": f"ACTION: {tool}\nARGS_JSON: {json.dumps(args)}"}


def _obs(text):
    return {"role": "user", "content": "OBSERVATION: " + text}


def test_middle_facts_lists_paths_and_the_first_error_line():
    middle = [_call("file_read", path="a.py"),
              _obs("ok"),
              _call("file_patch", path="b.py", old_text="x", new_text="y"),
              _obs("ok"),
              _call("run_command", cmd="pytest"),
              _obs("collected 3\nE   AssertionError: expected 2 got 3\nmore")]
    edited, read, errors = C._middle_facts(middle)
    assert edited == ["b.py"]
    assert read == ["a.py"]
    assert errors == ["E   AssertionError: expected 2 got 3"]


def test_the_note_carries_them_and_a_second_condense_keeps_them():
    tail = C._summary_tail(["ask"], [], (["b.py"], ["a.py"], ["boom Error"]))
    assert "Files edited: b.py" in tail and "Files read: a.py" in tail
    assert "Errors seen: boom Error" in tail
    note = C._breadcrumb([{}], "x×1", tail, "", 1)
    assert C._carry_prior_facts(note) == (["b.py"], ["a.py"], ["boom Error"])


def test_no_facts_leaves_the_note_as_before():
    assert C._summary_tail(["ask"], []) == "\nEarlier asks: ask"
