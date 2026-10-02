"""On a large window the first-message-only blocks go last, so the shared prefix
of the system prompt keeps the same bytes from message two on; a follow-up gets
the repo map only when it looks like code work."""
from aiforge_core.runtime.chat_agent._turn import _blocks as B


def test_a_follow_up_gets_the_repo_map_only_for_code_work(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_REPOMAP_EVERY_TURN", raising=False)
    assert B._needs_repo_map("fix the retry in src/app/client.py")
    assert B._needs_repo_map("update the parser module and add a test")
    assert not B._needs_repo_map("thanks, and why is the sky blue")
    monkeypatch.setenv("AIFORGE_CHAT_REPOMAP_EVERY_TURN", "1")
    assert B._needs_repo_map("why is the sky blue")


def test_first_message_blocks_go_last_when_the_window_is_ample(tmp_path):
    def run(ample):
        got = []
        B._append_context_blocks(lambda label, block: got.append(label),
                                 str(tmp_path), "fix src/a.py", [], None,
                                 "doer", False, ample=ample)
        return got
    ample, tight = run(True), run(False)
    for label in ("project-memory", "memory-index"):
        if label in tight:
            assert label in ample
            assert ample.index(label) > ample.index("repo-map") \
                if "repo-map" in ample else True
            assert ample[-1] in ("project-memory", "memory-index") or \
                ample.index(label) >= tight.index(label)
