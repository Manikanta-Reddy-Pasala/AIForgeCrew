"""Structured logging: the event shape, the redaction cap, and the LRU.

The per-ticket logger opens a FileHandler per ticket and evicts old ones so a
long-lived orchestrator does not accumulate open file descriptors. That
eviction is the kind of thing that only fails after a few hundred tickets, so
it is worth a test that drives the cap directly.
"""
from __future__ import annotations

import json
import logging

import pytest

from aiforge_core.observability import logging as L


@pytest.fixture(autouse=True)
def _isolated_logs(tmp_path, monkeypatch):
    """Point the module at a temp dir and start with an empty LRU.

    `_CONFIGURED` is reset too: `_configure_root()` is the only thing that
    creates LOG_DIR, and it runs once per process. Left alone, an earlier test
    in the session marks it configured, this fixture repoints LOG_DIR at a
    directory nobody creates, and the next FileHandler raises — which is
    exactly how this passed alone and failed in the full suite.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(L, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(L, "_run_logger_lru", [])
    monkeypatch.setattr(L, "_CONFIGURED", False)
    yield


# ── the JSON line ───────────────────────────────────────────────────────────

def _format(logger_name="t", **extra):
    rec = logging.LogRecord(logger_name, logging.INFO, "f.py", 1,
                            "the message", None, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return json.loads(L._JsonFormatter().format(rec))


def test_a_record_becomes_one_json_object():
    out = _format()
    assert out["event"] == "the message"
    assert out["level"] == "info"
    assert out["ts"].endswith("Z")


def test_the_aiforge_context_is_merged_into_the_line():
    out = _format(aiforge={"role": "doer", "ticket": "ONE-1"})
    assert out["role"] == "doer"
    assert out["ticket"] == "ONE-1"


# ── scrub ───────────────────────────────────────────────────────────────────

def test_a_short_value_survives_intact():
    assert L.scrub("hello") == "hello"


def test_a_long_value_is_capped():
    out = L.scrub("x" * 5000, limit=100)
    assert out == "x" * 100 + "\u2026"


@pytest.mark.parametrize("raw", [
    "doer\nINFO api: token accepted",
    "doer\r\nINFO api: token accepted",
    "doer\rINFO",
    "doer\x00INFO",
    "doer\x1b[31mINFO",
])
def test_a_value_cannot_forge_a_second_log_line(raw):
    """A newline in a value writes a SECOND line that reads exactly as
    authentic as the first. Everything crossing an HTTP boundary comes
    through here."""
    out = L.scrub(raw)
    for ch in ("\n", "\r", "\x00", "\x1b"):
        assert ch not in out


def test_scrub_accepts_anything_not_a_string():
    assert L.scrub(None)
    assert L.scrub({"a": 1})
    assert L.scrub(12345)


# ── emit ────────────────────────────────────────────────────────────────────

def test_emit_on_a_missing_logger_is_a_no_op():
    """Callers run from smoke scripts and tests without standing a logger up."""
    assert L.emit(None, "anything", a=1) is None


def test_emit_carries_the_loggers_role_and_ticket_plus_the_fields():
    seen = {}

    class _Log:
        _aiforge_role = "planner"
        _aiforge_ticket = "ONE-9"

        def info(self, event, extra=None):
            seen["event"] = event
            seen["ctx"] = extra["aiforge"]

    L.emit(_Log(), "stage.start", stage="plan", n=3)
    assert seen["event"] == "stage.start"
    assert seen["ctx"]["role"] == "planner"
    assert seen["ctx"]["ticket"] == "ONE-9"
    assert seen["ctx"]["stage"] == "plan"
    assert seen["ctx"]["n"] == 3


# ── per-role and per-ticket loggers ─────────────────────────────────────────

def test_get_logger_stamps_the_role_and_ticket():
    log = L.get_logger("verifier", ticket="ONE-2")
    assert log._aiforge_role == "verifier"
    assert log._aiforge_ticket == "ONE-2"


def test_a_run_logger_writes_one_ndjson_file_per_ticket(tmp_path):
    log = L.get_run_logger("ONE-3")
    log.info("hello", extra={"aiforge": {"k": "v"}})
    for h in log.handlers:
        h.flush()
    target = tmp_path / "runs" / "ONE-3.ndjson"
    assert target.is_file()
    line = json.loads(target.read_text().splitlines()[0])
    assert line["event"] == "hello"
    assert line["k"] == "v"


def test_a_ticket_id_with_separators_cannot_escape_the_runs_directory(tmp_path):
    """The id reaches this from a request; it becomes a FILENAME."""
    L.get_run_logger("../../etc/passwd")
    assert not (tmp_path.parent / "etc").exists()
    written = list((tmp_path / "runs").glob("*.ndjson"))
    assert len(written) == 1
    assert "/" not in written[0].name


def test_asking_twice_for_the_same_ticket_does_not_add_a_second_handler():
    a = L.get_run_logger("ONE-4")
    before = len(a.handlers)
    b = L.get_run_logger("ONE-4")
    assert a is b
    assert len(b.handlers) == before


def test_old_run_loggers_are_evicted_so_file_handles_do_not_accumulate(
        monkeypatch):
    """One never-closed FileHandler per ticket is a slow fd leak in an
    orchestrator that processes hundreds of them."""
    monkeypatch.setattr(L, "_RUN_LOGGER_CAP", 3)
    names = []
    for i in range(5):
        log = L.get_run_logger(f"ONE-{i}")
        names.append(log.name)
    assert len(L._run_logger_lru) == 3
    assert L._run_logger_lru == names[-3:]
    # The evicted ones are gone from the logging manager, handlers closed.
    for old in names[:2]:
        assert old not in logging.Logger.manager.loggerDict


def test_eviction_survives_a_handler_that_refuses_to_close(monkeypatch):
    monkeypatch.setattr(L, "_RUN_LOGGER_CAP", 1)
    first = L.get_run_logger("ONE-A")

    class _Stubborn(logging.Handler):
        def close(self):
            raise OSError("still open")

    first.addHandler(_Stubborn())
    L.get_run_logger("ONE-B")          # must not raise
    assert L._run_logger_lru == ["aiforge.run.ONE-B"]
