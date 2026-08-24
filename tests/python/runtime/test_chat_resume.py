"""Resuming a STOPPED turn instead of re-running it from scratch.

The retry used to re-send the same words and nothing else, so the agent redid
work that was already on disk. These pin the brief that fixes it — and, just
as important, the cases where it must produce NOTHING.

Most of these exist because a review proved the first cut read step shapes that
only ONE of the six producers emits.
"""
from aiforge_core.runtime import chat_resume as cr


def _stopped_row(steps=None, text="(stopped: hit the runaway safety cap)"):
    return {"role": "assistant", "content": text, "steps": steps or []}


def _rows(steps=None, prompt="build the thing"):
    return [{"role": "user", "content": prompt}, _stopped_row(steps)]


_LANDED = {"type": "tool", "name": "file_write", "args": {"path": "a.py"},
           "result": {"ok": True}}


# ── what counts as a stop ───────────────────────────────────────────────

def test_finished_turn_gets_no_brief():
    rows = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Done — added the endpoint.",
             "steps": [_LANDED]}]
    assert cr.last_stopped_turn(rows) is None
    assert cr.resume_preamble(rows, "hi") == ""


def test_the_structural_marker_is_what_decides():
    """A user Stop leaves NO banner — the loop emits an error step and only
    final_text becomes the content — so prose matching missed the exact case
    the Resume button exists for. chat_persist stamps a marker instead."""
    rows = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": "",           # nothing to match on
             "steps": [_LANDED, {"type": "stopped", "reason": "cancelled"}]}]
    assert cr.last_stopped_turn(rows) is not None
    assert "[RESUME]" in cr.resume_preamble(rows, "go")


def test_an_agent_quoting_a_stop_is_not_a_stop():
    """"stopped by user" is literally what run_command returns when cancelled,
    so an agent reporting its own tool output used to read as a stopped turn —
    and its next message would be told to "finish what is pending"."""
    for text in (
        "I ran `pytest -q` but it was stopped by user before it finished.",
        "The previous run stopped: hit the runaway safety cap, so I retried.",
    ):
        rows = [{"role": "user", "content": "x"},
                {"role": "assistant", "content": text, "steps": [_LANDED]}]
        assert cr.last_stopped_turn(rows) is None, text


def test_legacy_banner_turns_still_resume():
    """Turns persisted before the marker existed have only the banner."""
    assert cr.last_stopped_turn(_rows([_LANDED])) is not None


# ── what the brief says ─────────────────────────────────────────────────

def test_landed_attempted_and_pending_are_kept_apart():
    steps = [
        {"type": "tool", "name": "file_write", "args": {"path": "api/one.py"},
         "result": {"ok": True}},                                  # landed
        {"type": "tool", "name": "file_write", "args": {"path": "api/two.py"},
         "result": {"ok": False, "error": "disk full"}},           # NOT done
        # The ADK team pipeline sets every result to {"by": author} and reports
        # the real outcome separately — so "no error field" does not mean the
        # write landed. Claiming it did is how a resume drops a file for good.
        {"type": "tool", "role": "Doer", "name": "file_write",
         "args": {"path": "api/three.py"}, "result": {"by": "Doer"}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "pytest -q"},
         "result": {"ok": True}},
        {"type": "error", "text": "connection refused"},
    ]
    brief = cr.build_brief(_stopped_row(steps))
    assert "Already written" in brief
    assert "api/one.py" in brief
    assert "api/two.py" not in brief
    assert "outcome unknown" in brief
    assert "api/three.py" in brief
    assert "pytest -q" in brief
    assert "connection refused" in brief
    assert "do ONLY what is still missing" in brief


def test_subtasks_are_named_by_their_goal():
    """Five of the six subtask producers call the unit of work `goal`; one
    calls it `title`. Reading `title` alone produced "finish sub-2, sub-3"."""
    steps = [{"type": "subtasks", "items": [
        {"slug": "sub-1", "goal": "write the parser", "status": "done"},
        {"slug": "sub-2", "goal": "wire the CLI", "status": "running"},
        {"slug": "sub-3", "title": "add tests", "status": "pending"}]}]
    brief = cr.build_brief(_stopped_row(steps))
    assert "write the parser" in brief
    assert "wire the CLI" in brief
    assert "add tests" in brief
    assert "sub-2" not in brief


def test_terminal_statuses_are_not_reported_as_pending():
    steps = [{"type": "subtasks", "items": [
        {"slug": "a", "goal": "skipped one", "status": "skipped"},
        {"slug": "b", "goal": "the winner", "status": "won"},
        {"slug": "c", "goal": "planned one", "status": "planned"},
        {"slug": "d", "goal": "real work left", "status": "running"}]}]
    brief = cr.build_brief(_stopped_row(steps))
    pending = brief.split("Subtasks still PENDING:")[1]
    assert "real work left" in pending
    for gone in ("skipped one", "the winner", "planned one"):
        assert gone not in pending


def test_editor_view_is_a_read_not_an_edit():
    """`editor` multiplexes read and write on one tool name — every other
    consumer in the tree checks the sub-command, and this one must too, or a
    file the run only LOOKED at is declared already written."""
    steps = [{"type": "tool", "name": "editor",
              "args": {"command": "view", "path": "secret.py"},
              "result": {"ok": True}}]
    assert cr.build_brief(_stopped_row(steps)) == ""
    steps[0]["args"]["command"] = "str_replace"
    assert "secret.py" in cr.build_brief(_stopped_row(steps))


def test_edits_without_a_path_arg_are_still_found():
    """multi_edit keeps its paths in `edits`, the parallel runner keeps them in
    the RESULT, and other tools use file/target rather than path."""
    steps = [
        {"type": "tool", "name": "multi_edit",
         "args": {"edits": [{"path": "x.py"}, {"path": "y.py"}]},
         "result": {"ok": True}},
        {"type": "tool", "name": "wrote files", "args": {"subtask": "sub-1"},
         "result": {"files": ["z.py"]}},
        {"type": "tool", "name": "rename_symbol",
         "args": {"target": "w.py"}, "result": {"ok": True}},
    ]
    brief = cr.build_brief(_stopped_row(steps))
    for p in ("x.py", "y.py", "z.py", "w.py"):
        assert p in brief


# ── shape / safety ──────────────────────────────────────────────────────

def test_brief_is_empty_when_nothing_was_done():
    assert cr.build_brief(_stopped_row([])) == ""


def test_truncation_never_eats_the_instruction_or_the_errors():
    """Truncating the assembled string from the END deleted exactly the two
    blocks that make the brief useful — on the big runs that need it most."""
    steps = [{"type": "tool", "name": "file_write",
              "args": {"path": f"very/long/path/number/{i}/file.py"},
              "result": {"ok": True}} for i in range(500)]
    steps.append({"type": "error", "text": "the thing that killed it"})
    brief = cr.build_brief(_stopped_row(steps))
    assert len(brief) <= cr._MAX_BRIEF_CHARS
    assert brief.startswith("[RESUME]")
    assert "the thing that killed it" in brief
    assert "do ONLY what is still missing" in brief
    assert "and" in brief
    assert "more" in brief


def test_preamble_only_when_the_same_ask_is_resent():
    rows = _rows([_LANDED])
    assert cr.resume_preamble(rows, "build the thing") != ""     # retry
    assert cr.resume_preamble(rows, "now do something else") == ""
    assert cr.resume_preamble(rows, "finish it please", forced=True) != ""


def test_false_forces_a_clean_rerun():
    """The partial work may be junk the user wants abandoned. Without an
    explicit opt-out, every route back to "run this again" meant "continue"."""
    rows = _rows([_LANDED])
    assert cr.resume_preamble(rows, "build the thing", forced=False) == ""


def test_a_broken_steps_payload_never_raises():
    """Steps come off disk and out of models. A resume must degrade to
    nothing, never take the turn down — and never vanish silently because a
    field held an int where a string was assumed."""
    assert cr.build_brief({"content": "(stopped: x)", "steps": "not-a-list"}) == ""
    assert cr.build_brief({"content": "(stopped: x)",
                           "steps": [None, 3, {"type": "tool"}]}) == ""
    assert "123" in cr.build_brief(
        {"content": "(stopped: x)", "steps": [{"type": "error", "text": 123}]})
    assert cr.resume_preamble([], "anything") == ""
    assert cr.resume_preamble([None, "junk"], "anything") == ""
