# tests/python/runtime/test_targets.py
import json
import logging
import os

import pytest

from aiforge_core.runtime import targets as T

JSON = '{"a": 1, "b": [1, 2, 3], "c": {"d": "e"}}' + " " * 200


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_TARGETS", "1")
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))


def test_cue_plus_fenced_block_is_captured(tmp_path):
    msg = "The endpoint must match this output:\n```json\n" + JSON + "\n```"
    got = T.capture(5, msg)
    assert len(got) == 1
    t = got[0]
    assert t["id"] == "target-1" and t["label"] == "output" and t["path"].endswith("target-1.json")
    assert open(t["path"]).read().strip() == JSON.strip()
    assert T.list(5)[0]["verified"] is None
    assert json.loads(open(os.path.join(T.dir(5), "targets.json")).read())[0]["turn"] == 1


def test_cue_plus_long_paste_without_fence(tmp_path):
    msg = "expected output:\n" + "x,y\n" * 120
    got = T.capture(5, msg)
    assert got[0]["path"].endswith(".csv")


def test_explicit_target_form_works_for_short_payloads():
    got = T.capture(5, "set target: output = ```\nhello\n```")
    assert got and got[0]["label"] == "output"


@pytest.mark.parametrize("msg", [
    "match this output",                       # cue, no payload
    "```\n" + JSON + "\n```",                  # payload, no cue
    "please compare the two files",            # neither
    "the expected output is wrong, fix it",    # cue words, no payload
])
def test_no_capture_without_cue_and_payload(msg):
    assert T.capture(5, msg) == []
    assert T.list(5) == []


def test_same_label_again_makes_a_new_version():
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    T.mark_verified(5, "target-1", "diff ok")
    got = T.capture(5, "actually match this output instead:\n```\n" + JSON.replace("1", "2") + "\n```")
    assert got[0]["id"] == "target-1"
    rows = T.list(5)
    assert len(rows) == 1 and rows[0]["path"].endswith("target-1.v2.json")
    assert rows[0]["verified"] is None and len(rows[0]["versions"]) == 2


def test_different_label_is_a_second_target():
    T.capture(5, "match this input:\n```\n" + JSON + "\n```")
    T.capture(5, "and match this output:\n```\n" + JSON + "\n```")
    assert [t["id"] for t in T.list(5)] == ["target-1", "target-2"]


def test_block_lists_targets_with_path_and_preview():
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    b = T.block(5)
    assert b.startswith("TARGETS")
    assert "target-1" in b and "output" in b and "file_read" in b and '{"a": 1' in b
    assert "Never ask the user to paste" in b and "verified: no" in b
    assert T.block(6) == ""


def test_disabled_switch(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_TARGETS", "0")
    assert T.capture(5, "match this output:\n```\n" + JSON + "\n```") == []
    assert T.block(5) == ""


@pytest.mark.parametrize("cmd,ok", [
    ("diff out.json {p}", True), ("diff {p} out.json", True), ("cmp a {p}", True),
    ("jq -S . {p} > a && jq -S . out.json > b && diff a b", True),
    ("python compare.py {p} out.json", True), ("python -c 'import json; assert json.load(open(\"{p}\")) == x'", True),
    ("sort out.csv | diff - {p}", True), ("git diff --no-index {p} out.json", True),
    ("cat {p}", False), ("diff a b", False), ("ls", False), ("python run.py", False),
    ("jq . {p}", False), ("jq -S . {p} > a", False),
])
def test_is_compare(cmd, ok):
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    p = T.list(5)[0]["path"]
    assert T.is_compare(cmd.format(p=p), 5) == ("target-1" if ok else None)
    assert T.is_compare(cmd.format(p=os.path.relpath(p)), 5) == ("target-1" if ok else None)


def test_mark_verified_records_evidence():
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    T.mark_verified(5, "target-1", "diff -q: no output")
    assert T.list(5)[0]["verified"]["evidence"] == "diff -q: no output"
    assert "verified: yes" in T.block(5)


@pytest.mark.parametrize("text,hit", [
    ("Can you paste the expected output again?", True),
    ("Please share the expected JSON so I can compare.", True),
    ("What was the data you gave earlier?", True),
    ("Send me a sample input.", True),
    ("Done: output matches target-1 (diff empty).", False),
    ("I pasted the result below.", False),
])
def test_reask_regex(text, hit):
    assert bool(T.asks_for_target(text)) is hit


def test_board_item_for_a_target():
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    items = T.board_items(5)
    p = T.list(5)[0]["path"]
    assert os.path.isabs(p) and p.endswith("/targets/target-1.json")
    assert items == {"target-1": {"title": "output matches target-1 — check: a compare command on "
                                           f"{p} ran and passed",
                                  "status": "pending", "enforced": True, "proof": "target-1"}}
    assert f"file_read {p}" in T.block(5)
    T.mark_verified(5, "target-1", "ok")
    assert T.board_items(5) == {}


# --- beyond the plan: paste cap, prose cues, index robustness ---------------

def test_huge_paste_is_capped_and_flagged():
    big = "x" * (3 * 1024 * 1024)
    got = T.capture(5, "match this output:\n```\n" + big + "\n```")
    row = got[0]
    assert row["truncated"] is True
    assert os.path.getsize(row["path"]) <= T.MAX_BYTES + 1
    assert row["bytes"] == os.path.getsize(row["path"])
    small = T.capture(5, "match this input:\n```\n" + JSON + "\n```")
    assert small[0]["truncated"] is False


def test_cue_in_prose_followed_by_long_prose_is_not_a_target():
    msg = "the expected output is wrong, fix it\n" + "please look at the handler and " * 12
    assert T.capture(5, msg) == []
    assert T.list(5) == []


def test_cue_words_inside_the_fence_do_not_add_a_second_target():
    body = '{"expected output": "match this data", "k": "' + "v" * 300 + '"}'
    got = T.capture(5, "match this output:\n```\n" + body + "\n```")
    assert [r["id"] for r in got] == ["target-1"]
    assert T.list(5)[0]["label"] == "output"


def test_noun_after_must_match_sets_the_label():
    got = T.capture(5, "The parser must match this input:\n```\n" + JSON + "\n```")
    assert got[0]["label"] == "input"


def test_is_compare_ignores_old_versions_and_other_files_with_the_name():
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    T.capture(5, "match this output:\n```\n" + JSON.replace("1", "2") + "\n```")
    rows = T.list(5)
    v1, v2 = rows[0]["versions"]
    assert T.is_compare(f"diff out.json {v1}", 5) is None
    assert T.is_compare(f"diff out.json {v2}", 5) == "target-1"
    assert T.is_compare("diff out.json target-1.v2.json", 5) == "target-1"
    assert T.is_compare("diff out.json ./target-1.v2.json", 5) == "target-1"
    assert T.is_compare("diff out.json other/target-1.v2.json", 5) is None
    assert T.is_compare("cat target-1.v2.json | head", 5) is None


# --- review round 1 ----------------------------------------------------------

def test_unfenced_payload_after_a_prose_tail_starts_on_the_next_line():
    got = T.capture(5, "The parser must match this input exactly\n" + "x,y\n" * 120)
    assert got[0]["label"] == "input" and got[0]["path"].endswith(".csv")
    with open(got[0]["path"]) as fh:
        assert fh.read() == "x,y\n" * 120


@pytest.mark.parametrize("msg", [
    "write it to the target file, then run:\n```\nmake test\n```",
    "the result should be exactly like before. Run:\n```bash\nmake test\n```",
    "the output should look like the input\n"
    + "It reads the config. Then it opens the socket. It writes the rows. " * 6,
    "match this output, but only the first two keys, see below:\n```\n" + JSON + "\n```",
])
def test_prose_cue_lines_and_prose_pastes_are_not_targets(msg):
    assert T.capture(5, msg) == []
    assert T.list(5) == []


def test_csv_paste_after_a_colon_still_captured():
    got = T.capture(5, "expected output:\n" + "x,y\n" * 120)
    assert got and got[0]["path"].endswith(".csv")


def test_note_compare_differs_then_matched():
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    T.note_compare(5, "target-1", False, "1c1\n< a\n> b")
    row = T.list(5)[0]
    assert row["verified"] is None
    assert row["last_compare"]["ok"] is False and row["last_compare"]["evidence"].startswith("1c1")
    assert T.facts(5) == ["target-1: differs"]
    assert "verified: no (last compare differed)" in T.block(5)
    assert "target-1" in T.board_items(5)
    T.note_compare(5, "target-1", True, "diff -q: no output")
    row = T.list(5)[0]
    assert row["verified"]["evidence"] == "diff -q: no output" and row["last_compare"]["ok"] is True
    assert T.facts(5) == ["target-1: matched"] and T.board_items(5) == {}
    # a new version of the target forgets the old compare
    T.capture(5, "match this output:\n```\n" + JSON.replace("1", "2") + "\n```")
    assert T.list(5)[0]["last_compare"] is None and T.facts(5) == ["target-1: not yet compared"]


def test_read_drops_rows_missing_required_keys():
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    idx = os.path.join(T.dir(5), "targets.json")
    with open(idx) as fh:
        rows = json.load(fh)
    rows += [{"id": "target-9"}, {"label": "x", "path": "/nowhere"}, "junk",
             {"id": "t", "label": "l"}]
    with open(idx, "w") as fh:
        json.dump(rows, fh)
    assert [r["id"] for r in T.list(5)] == ["target-1"]
    assert T.block(5).count("\n- ") == 1 and list(T.board_items(5)) == ["target-1"]


def test_cue_after_the_fence_may_end_with_a_period():
    got = T.capture(5, "```\n" + JSON + "\n```\nThe API must match this output.")
    assert got and got[0]["label"] == "output"
    msg = "```\n" + JSON + "\n```\nThe API must match this output, then deploy."
    assert T.capture(6, msg) == []


def test_unreadable_index_reads_empty_logs_once_and_refuses_to_write(caplog):
    caplog.set_level(logging.WARNING, logger="aiforge.targets")
    os.makedirs(os.path.join(T.dir(5), "targets.json"))  # a directory where the file should be
    assert T.list(5) == []
    assert T.list(5) == []
    assert T.block(5) == ""
    assert sum("cannot read" in r.getMessage() for r in caplog.records) == 1
    with pytest.raises(OSError):
        T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    with pytest.raises(OSError):
        T.mark_verified(5, "target-1", "ok")
    assert os.path.isdir(os.path.join(T.dir(5), "targets.json"))


def test_corrupt_index_is_moved_aside_not_overwritten(caplog):
    caplog.set_level(logging.WARNING, logger="aiforge.targets")
    os.makedirs(T.dir(5))
    idx = os.path.join(T.dir(5), "targets.json")
    with open(idx, "w") as fh:
        fh.write("{not json")
    assert T.list(5) == []
    got = T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    assert got[0]["id"] == "target-1"
    bad = [n for n in os.listdir(T.dir(5)) if n.startswith("targets.json.bad-")]
    assert len(bad) == 1
    with open(os.path.join(T.dir(5), bad[0])) as fh:
        assert fh.read() == "{not json"
    assert len(T.list(5)) == 1


@pytest.mark.parametrize("text,hit", [
    ("Could you paste the expected JSON?", True),
    ("Please resend the sample file.", True),
    ("I'll paste the expected output here for reference.", False),
    ("The output matches the expected data; diff is empty.", False),
])
def test_reask_regex_request_forms_only(text, hit):
    assert bool(T.asks_for_target(text)) is hit


def test_mark_verified_unknown_id_does_not_touch_the_index():
    T.capture(5, "match this output:\n```\n" + JSON + "\n```")
    idx = os.path.join(T.dir(5), "targets.json")
    with open(idx) as fh:
        before = fh.read()
    T.mark_verified(5, "target-9", "ok")
    with open(idx) as fh:
        assert fh.read() == before
    assert T.facts(5) == ["target-1: not yet compared"]
    T.mark_verified(5, "target-1", "ok")
    assert T.facts(5) == ["target-1: matched"]


@pytest.mark.parametrize("msg", [
    "Change report.py so that running `python3 -m calc.report 6 3` prints exactly this output "
    "(keys in this order, values rounded to 2 decimals, plus a pow key = a**b):\n"
    "```\n{\"add\": 9.0, \"pow\": 216.0}\n```",
    "make the endpoint return exactly this json:\n```\n{\"ok\": true}\n```",
    "it should output exactly:\n```\nA,B\n1,2\n```",
])
def test_a_short_fenced_payload_after_an_exact_cue_line_is_a_target(msg):
    """A fenced payload counts at any size; a cue line that runs on and ends
    in ":" introduces it, unless it makes the match partial."""
    got = T.capture(5, msg)
    assert len(got) == 1, msg


def test_a_hedged_long_cue_line_is_still_not_a_target():
    msg = ("it prints exactly the same thing apart from the timestamp, roughly like this:\n"
           "```\n{\"a\": 1}\n```")
    assert T.capture(5, msg) == []
