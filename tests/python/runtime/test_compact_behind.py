"""The model summary must not sit in front of the agent turn."""
import time


def _long_convo():
    convo = [{"role": "system", "content": "S" * 100}]
    for _ in range(30):
        convo.append({"role": "assistant",
                      "content": "THOUGHT: t\nACTION: file_read\nARGS_JSON: {}"})
        convo.append({"role": "user", "content": "OBSERVATION: " + "x" * 200})
    return convo


def test_llm_compact_returns_before_the_model_answers(monkeypatch):
    import threading
    from aiforge_core.runtime.chat_agent._context import _compaction as c

    monkeypatch.setenv("AIFORGE_COMPACT_MODE", "llm")
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "2000")
    monkeypatch.setenv("AIFORGE_CONDENSE_TIMEOUT_S", "5")
    started = threading.Event()
    release = threading.Event()
    seen_role = {}

    def slow(role, _messages):
        seen_role["role"] = role
        started.set()
        release.wait(5)
        return "SUMMARY FROM MODEL"

    c._SUMMARY_READY.clear()
    c._SUMMARY_BUSY.clear()
    try:
        t0 = time.monotonic()
        out = c._compact_convo(_long_convo(), keep_recent=8,
                               complete_fn=slow, session_id="s-behind")
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        assert "auto-condensed" in out[0]["content"]
        assert "SUMMARY FROM MODEL" not in out[0]["content"]
        assert started.wait(2)
        assert seen_role["role"] == "learner"
        release.set()
        text = ""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not text:
            text = c._take_ready_summary("s-behind")
            if not text:
                time.sleep(0.05)
        assert text == "SUMMARY FROM MODEL"
    finally:
        release.set()
        c._SUMMARY_READY.clear()
        c._SUMMARY_BUSY.clear()


def test_background_summary_does_not_use_the_turns_tool_queue(monkeypatch):
    """The native complete_fn owns the turn's queued reads. The summary
    must call the plain client instead."""
    import threading
    from aiforge_core.runtime.chat_agent._context import _compaction as c

    monkeypatch.setenv("AIFORGE_COMPACT_MODE", "llm")
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "2000")
    called = threading.Event()

    def native(role, _messages):
        called.set()
        return "SHOULD NOT RUN"

    native.take_queued = lambda: None
    seen = {}

    def plain(role, _messages):
        seen["role"] = role
        return "PLAIN SUMMARY"

    monkeypatch.setattr(
        "aiforge_core.llm.client.complete", plain, raising=False)
    c._SUMMARY_READY.clear()
    c._SUMMARY_BUSY.clear()
    try:
        c._compact_convo(_long_convo(), keep_recent=8,
                         complete_fn=native, session_id="s-native")
        deadline = time.monotonic() + 3
        text = ""
        while time.monotonic() < deadline and not text:
            text = c._take_ready_summary("s-native")
            if not text:
                time.sleep(0.05)
        assert text == "PLAIN SUMMARY"
        assert seen["role"] == "learner"
        assert not called.is_set()
    finally:
        c._SUMMARY_READY.clear()
        c._SUMMARY_BUSY.clear()
