"""A message sent mid-run reaches the running model as the user's own words,
under a short header that says they arrived while it was working.

The model working on the task decides what the message means; the wrapper does
not classify it. These pin the wrapper and that it reaches the model.
"""
from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime import chat_interject, chat_steer

_SID = 771_004


def test_the_directive_is_a_light_wrapper_around_the_users_words():
    """The running model reads the message and decides what it means. The
    wrapper says where the words come from and that the task goes on unless
    they say otherwise — it does not tell the model how to classify them."""
    d = chat_steer.steer_directive("use postgres instead")
    assert d.startswith("[NEW MESSAGE FROM THE USER — sent while you were working.]")
    assert "use postgres instead" in d
    assert "continue with the task you were on unless it says otherwise" in d
    assert len(d.splitlines()) == 3 and len(d) < 220
    # no instruction on how to read it
    assert "REPLACES" not in d and "ADDS" not in d and "PRIORITY" not in d


def test_a_mid_run_message_reaches_the_model_as_that_directive(tmp_path):
    """End to end through the ReAct loop: what the model actually sees."""
    seen: list = []

    def fn(role, messages, **kw):
        seen.append(messages)
        if len(seen) == 1:
            # Queue the steer as if the user typed it during this step.
            chat_interject.push(_SID, "stop that, answer this instead")
            return 'ACTION: file_read\nARGS_JSON: {"path": "a.txt"}'
        return "done"

    (tmp_path / "a.txt").write_text("hi")
    chat_interject.clear(_SID)
    list(ca.run_chat_agent([{"role": "user", "content": "original ask"}],
                           cwd=str(tmp_path), complete_fn=fn, session_id=_SID))
    folded = "\n".join(m.get("content") or "" for m in seen[-1]
                       if m.get("role") == "user")
    assert "stop that, answer this instead" in folded
    assert "sent while you were working" in folded
    chat_interject.clear(_SID)


def test_several_steers_drain_as_one_ordered_block():
    """Three queued messages produced three blocks each claiming to be THE
    most recent instruction, with no ordering signal — "use postgres" and the
    "actually no, sqlite" a second later arrived as equals."""
    block = chat_steer.steer_block(["use postgres", "actually no, sqlite"])
    assert block.count("NEW MESSAGE FROM THE USER") == 1
    assert "1. use postgres" in block and "2. actually no, sqlite" in block
    assert block.index("use postgres") < block.index("actually no, sqlite")
    assert block.splitlines()[2].endswith("the latest")


def test_a_rejection_is_not_a_steer():
    """Approval-card guidance corrects the REJECTED ACTION; it is never a new
    task. Wrapped in the steer wording it told the agent it could abandon the
    request — a "use tmp/ instead" dropped the rest of a half-built feature."""
    note = chat_steer.reject_note("use tmp/ instead")
    assert "CONTINUE the current task" in note
    assert "abandon" not in note.lower()
    assert "PRIORITY" not in note


def test_a_steer_does_not_splat_into_a_multimodal_turn(tmp_path):
    """A vision turn's content is a LIST of parts. `list += str` extends it
    with the string's CHARACTERS: one steer became 364 single-character parts,
    invisible to _text_of (so the condenser and the context meter both missed
    it) while still being sent."""
    seen: list = []

    def fn(role, messages, **kw):
        seen.append([dict(m) for m in messages])
        return "done"

    (tmp_path / "a.txt").write_text("hi")
    chat_interject.clear(_SID)
    # Queued BEFORE the run: it drains on the first iteration, when the last
    # turn is still the vision message — the exact window where the merge
    # touches list content.
    chat_interject.push(_SID, "actually use sqlite")
    vision_turn = {"role": "user", "content": [
        {"type": "text", "text": "what is in this image?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}
    list(ca.run_chat_agent([vision_turn], cwd=str(tmp_path),
                           complete_fn=fn, session_id=_SID))
    parts = [p for m in seen[-1] if isinstance(m.get("content"), list)
             for p in m["content"]]
    assert parts, "the vision turn lost its list content"
    assert all(isinstance(p, dict) for p in parts), \
        f"content was splatted into characters: {parts[:6]}"
    assert any(p.get("type") == "text" and "actually use sqlite" in p.get("text", "")
               for p in parts)
    chat_interject.clear(_SID)
