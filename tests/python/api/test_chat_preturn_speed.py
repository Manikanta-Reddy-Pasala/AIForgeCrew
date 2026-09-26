"""Before the agent speaks: a long request's rule capture runs off the critical
path, an answer/review request skips the enhancer, and vision probes for the
same model share one request."""
from __future__ import annotations

import threading
import time
import types

import pytest

from aiforge_core.api.routes._chat import _capture_bg
from aiforge_core.api.routes._chat._routing import _should_skip_enhance

LONG = ("Please review the design of the fmt function and the tests. I want a "
        "short written assessment covering rounding, negatives, lakh and crore "
        "grouping, floats versus Decimal, and a recommendation. Do not change "
        "any files; just answer in prose with a short bullet list at the end.")


# ── enhancer skip ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("cat", ["chat", "doc_analysis"])
def test_long_answer_request_skips_enhancer(cat, monkeypatch):
    monkeypatch.delenv("AIFORGE_SKIP_ENHANCE_ANSWERS", raising=False)
    assert _should_skip_enhance(False, False, False, None, LONG * 3, cat=cat)


@pytest.mark.parametrize("cat", ["code_build", "code_edit", "tracker", None])
def test_long_change_request_still_enhances(cat):
    assert not _should_skip_enhance(False, False, False, None, LONG * 3, cat=cat)


def test_pipeline_route_never_skips():
    assert not _should_skip_enhance(False, True, False, None, LONG, cat="chat")


def test_answer_skip_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_SKIP_ENHANCE_ANSWERS", "0")
    assert not _should_skip_enhance(False, False, False, None, LONG * 3,
                                    cat="doc_analysis")


# ── background capture ────────────────────────────────────────────────────

def test_inline_only_for_short_messages():
    assert _capture_bg.inline_needed("always use tabs from now on")
    assert not _capture_bg.inline_needed(LONG)
    assert not _capture_bg.inline_needed("a\nb\nc\nd")


class _FakeRC:
    def __init__(self, gate: threading.Event | None = None):
        self.gate = gate
        self.stored = []

    def should_classify(self, _p):
        return True

    def repo_key(self, _cwd):
        return "repo"

    def classify(self, prompt, *, repo=None, session_id=None):
        if self.gate is not None:
            self.gate.wait(5)
        return {"category": "rule", "scope": "project",
                "canonical": "never change files in a review",
                "task_present": True}

    def store(self, cls, **_kw):
        self.stored.append(cls)
        return {"id": "r1"}

    def recognize_gate_intent(self, _cls):
        return None


class _Run:
    def __init__(self):
        self.events, self.done = [], False

    def publish(self, ev):
        if not self.done:
            self.events.append(ev)


def _pc():
    return types.SimpleNamespace(prompt=LONG, cwd="/tmp", session_id="s1",
                                 run=_Run())


def _eventually(fn, secs=5.0):
    deadline = time.time() + secs
    while not fn() and time.time() < deadline:
        time.sleep(0.02)
    return fn()


def _install(monkeypatch, rc):
    import sys

    import aiforge_core.runtime as runtime_pkg
    monkeypatch.setattr(runtime_pkg, "rule_capture", rc, raising=False)
    monkeypatch.setitem(sys.modules, "aiforge_core.runtime.rule_capture", rc)


def test_nothing_is_sent_before_the_agent_answers(monkeypatch):
    rc = _FakeRC()
    _install(monkeypatch, rc)
    pc = _pc()
    _capture_bg.begin(pc)
    assert pc._bg_capture is not None and pc._bg_capture.future is None
    _capture_bg.kick(pc)                            # the agent's done
    evs = list(_capture_bg.events(pc))              # never waits (default 0)
    _capture_bg.flush(pc)
    assert _eventually(lambda: len(rc.stored) == 1)
    got = evs + pc.run.events                       # inline or published
    assert got and got[0]["type"] == "captured" and got[0]["id"] == "r1"
    time.sleep(0.1)
    assert len(rc.stored) == 1


def test_turn_without_agent_done_still_stores_on_flush(monkeypatch):
    rc = _FakeRC()
    _install(monkeypatch, rc)
    pc = _pc()
    _capture_bg.begin(pc)
    _capture_bg.flush(pc)                           # another route ended the turn
    deadline = time.time() + 5
    while not rc.stored and time.time() < deadline:
        time.sleep(0.02)
    assert len(rc.stored) == 1


def test_slow_classify_waits_only_the_tail_budget(monkeypatch):
    monkeypatch.setenv("AIFORGE_CAPTURE_TAIL_WAIT_S", "0.2")
    gate = threading.Event()
    rc = _FakeRC(gate)
    _install(monkeypatch, rc)
    pc = _pc()
    _capture_bg.begin(pc)
    _capture_bg.kick(pc)
    t = time.time()
    assert list(_capture_bg.events(pc)) == []       # did not land in time
    _capture_bg.flush(pc)
    assert time.time() - t < 1.0
    gate.set()
    deadline = time.time() + 5
    while not rc.stored and time.time() < deadline:
        time.sleep(0.02)
    assert len(rc.stored) == 1


def test_no_cue_no_capture(monkeypatch):
    rc = _FakeRC()
    rc.should_classify = lambda _p: False
    _install(monkeypatch, rc)
    pc = _pc()
    _capture_bg.begin(pc)
    assert pc._bg_capture is None


# ── early enhance beside the classifier ─────────────────────────────────────

def test_read_only_requests_do_not_start_an_early_enhance():
    from aiforge_core.api.routes._chat import _overlap
    assert _overlap._reads_as_answer(LONG)
    assert _overlap._reads_as_answer("how should invoices be rounded")
    assert not _overlap._reads_as_answer(
        "build a billing service with an invoices API, a database and tests")


# ── vision probe single-flight ──────────────────────────────────────────────

def test_concurrent_vision_probes_share_one_request(monkeypatch):
    from aiforge_core.runtime import vision_detect as vd
    calls = []
    release = threading.Event()

    def _once(model, base_url, api_key=None, *, timeout_s=None):
        calls.append(model)
        release.wait(5)
        return True
    monkeypatch.setattr(vd, "_probe_endpoint_once", _once)
    out = []
    ths = [threading.Thread(target=lambda: out.append(
        vd.probe_vision_endpoint("m", "http://s/v1"))) for _ in range(2)]
    for th in ths:
        th.start()
    time.sleep(0.2)
    release.set()
    for th in ths:
        th.join(5)
    assert calls == ["m"] and out == [True, True]
    # A later probe runs again (nothing left in flight).
    assert vd.probe_vision_endpoint("m", "http://s/v1") is True
    assert calls == ["m", "m"]


# ── review r2: capture after Stop / during the next turn ───────────────────

def test_a_stopped_turn_stores_no_rule(monkeypatch):
    from aiforge_core.runtime import chat_cancel
    rc = _FakeRC()
    _install(monkeypatch, rc)
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: True)
    pc = _pc()
    _capture_bg.start_turn(pc)
    _capture_bg.begin(pc)
    _capture_bg.kick(pc)
    assert list(_capture_bg.events(pc)) == []
    _capture_bg.flush(pc)
    time.sleep(0.2)
    assert rc.stored == []


def test_a_capture_landing_mid_next_turn_waits_for_it_to_end(monkeypatch):
    gate = threading.Event()
    rc = _FakeRC(gate)
    _install(monkeypatch, rc)
    pc = _pc()
    _capture_bg.start_turn(pc)
    _capture_bg.begin(pc)
    _capture_bg.kick(pc)
    list(_capture_bg.events(pc))
    _capture_bg.flush(pc)                           # turn 1 over, classify slow
    pc2 = _pc()
    _capture_bg.start_turn(pc2)                     # turn 2 of the same chat
    gate.set()
    time.sleep(0.3)
    assert rc.stored == []                          # not mid-run
    _capture_bg.flush(pc2)
    assert _eventually(lambda: len(rc.stored) == 1)


def test_the_capture_classify_is_a_side_call(monkeypatch):
    from aiforge_core.llm import model_wait
    seen = []

    class RC(_FakeRC):
        def classify(self, prompt, **kw):
            seen.append(model_wait._OPTIONAL.get())
            return super().classify(prompt, **kw)
    rc = RC()
    _install(monkeypatch, rc)
    pc = _pc()
    _capture_bg.begin(pc)
    _capture_bg.kick(pc)
    assert _eventually(lambda: seen)
    assert seen == [True]


# ── review r2: the enhancer skip needs more than one triage word ───────────

EDIT = ("Add a --dry-run flag to the CLI in cli.py, update the README to "
        "describe it, fix the failing test in tests/test_cli.py and rename "
        "the helper parse_args to build_parser across the package. " * 2)


@pytest.mark.parametrize("cat", ["chat", "doc_analysis"])
def test_a_long_edit_request_labelled_chat_keeps_the_enhancer(cat):
    assert not _should_skip_enhance(False, False, False, None, EDIT, cat=cat)


def test_review_as_a_noun_in_an_edit_request_is_not_read_only():
    from aiforge_core.api.routes._chat import _overlap
    assert not _overlap._reads_as_answer("add a review widget to the dashboard")
    assert _overlap._reads_as_answer("Please review the auth module.")


# ── review r2: vision single-flight ─────────────────────────────────────────

def test_vision_probes_with_different_keys_do_not_share(monkeypatch):
    from aiforge_core.runtime import vision_detect as vd
    calls = []
    release = threading.Event()

    def _once(model, base_url, api_key=None, *, timeout_s=None):
        calls.append(api_key)
        release.wait(5)
        return api_key == "good"
    monkeypatch.setattr(vd, "_probe_endpoint_once", _once)
    out = {}
    ths = [threading.Thread(target=lambda k=k: out.__setitem__(
        k, vd.probe_vision_endpoint("m", "http://s/v1", k))) for k in ("good", "bad")]
    for th in ths:
        th.start()
    time.sleep(0.2)
    release.set()
    for th in ths:
        th.join(5)
    assert sorted(calls) == ["bad", "good"]
    assert out == {"good": True, "bad": False}


def test_a_vision_waiter_waits_for_the_owner_past_its_timeout(monkeypatch):
    from aiforge_core.runtime import vision_detect as vd
    release = threading.Event()
    calls = []

    def _once(model, base_url, api_key=None, *, timeout_s=None):
        calls.append(1)
        release.wait(10)
        return True
    monkeypatch.setattr(vd, "_probe_endpoint_once", _once)
    out = []
    owner = threading.Thread(target=lambda: out.append(
        vd.probe_vision_endpoint("m", "http://s/v1", timeout_s=0)))
    owner.start()
    time.sleep(0.1)
    waiter = threading.Thread(target=lambda: out.append(
        vd.probe_vision_endpoint("m", "http://s/v1", timeout_s=0)))
    waiter.start()
    time.sleep(0.5)
    assert waiter.is_alive()                   # still waiting, not "None"
    release.set()
    owner.join(5)
    waiter.join(5)
    assert out == [True, True] and calls == [1]
