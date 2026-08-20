"""ONE local memory compaction a day, in the evening (AIFORGE_COMPACT_AT_HOUR).

Every fold costs learner-LLM calls, so the hourly chat-compact + the per-idle
-session fold + the 02:00 recompact are collapsed into one evening pass.
"""
from __future__ import annotations

import importlib
from datetime import date

import pytest


# Every knob the registration path reads — an operator's own value in the
# environment must not decide what these assert.
_KNOBS = ("AIFORGE_JOBS_DISABLE", "AIFORGE_REINDEX_DAILY", "AIFORGE_REINDEX_EVERY_H",
          "AIFORGE_REINDEX_HOUR", "AIFORGE_COMPACT_EVERY_H", "AIFORGE_COMPACT_AT_HOUR",
          "AIFORGE_RECOMPACT_DAILY", "AIFORGE_RECOMPACT_HOUR", "AIFORGE_SESSION_COMPACT",
          "AIFORGE_SESSION_IDLE_MIN", "AIFORGE_SESSION_COMPACT_MAX_WINDOWS")


def _api(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    for k in _KNOBS:
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return api


def _registered(monkeypatch, tmp_path, **env):
    """Task names _start_daily_reindex registers, without running any of them."""
    api = _api(monkeypatch, tmp_path)
    for k, v in env.items():                 # after _api — it clears the knobs
        monkeypatch.setenv(k, v)
    from aiforge_core.runtime import periodic as p
    monkeypatch.setattr(p, "_TASKS", [])
    monkeypatch.setattr(p, "start", lambda: None)
    api._start_daily_reindex()
    return {t.name: t for t in p._TASKS}


@pytest.mark.parametrize("raw,want", [
    (None, 18), ("18", 18), ("20", 20), ("24", 0),      # 24 = midnight
    ("off", None), ("", None), ("none", None),
    ("0", None), ("-3", None),                           # 0 = off, like the siblings
    ("nonsense", 18), ("99", 23),
])
def test_compact_at_hour_parsing(monkeypatch, tmp_path, raw, want):
    api = _api(monkeypatch, tmp_path)
    if raw is None:
        monkeypatch.delenv("AIFORGE_COMPACT_AT_HOUR", raising=False)
    else:
        monkeypatch.setenv("AIFORGE_COMPACT_AT_HOUR", raw)
    assert api._compact_at_hour() == want


def test_explicit_interval_opts_out_of_the_daily_pass(monkeypatch, tmp_path):
    api = _api(monkeypatch, tmp_path)
    monkeypatch.setenv("AIFORGE_COMPACT_EVERY_H", "1")
    assert api._compact_at_hour() is None
    # …but an explicit hour still wins over it
    monkeypatch.setenv("AIFORGE_COMPACT_AT_HOUR", "18")
    assert api._compact_at_hour() == 18


def test_default_registers_one_evening_task(monkeypatch, tmp_path):
    tasks = _registered(monkeypatch, tmp_path)
    assert "daily-compact" in tasks
    assert tasks["daily-compact"].at_hour == 18
    assert tasks["daily-compact"].every_s is None
    # the three separate jobs it replaces are gone
    for gone in ("chat-compact", "session-okr-compact", "recompact-all"):
        assert gone not in tasks


def test_off_restores_the_old_three_jobs(monkeypatch, tmp_path):
    tasks = _registered(monkeypatch, tmp_path, AIFORGE_COMPACT_AT_HOUR="off")
    assert "daily-compact" not in tasks
    assert tasks["chat-compact"].every_s == 3600
    assert tasks["session-okr-compact"].every_s == 30 * 60
    assert tasks["recompact-all"].at_hour == 2


def test_daily_pass_runs_sessions_then_briefs_then_recompact(monkeypatch, tmp_path):
    """Order matters: a day's chat must reach its brief in the SAME pass."""
    calls: list = []
    import aiforge_core.runtime.chat_okr as chat_okr
    import aiforge_core.runtime.chat_store as chat_store
    from aiforge_core.memory import md_store, migrations
    monkeypatch.setattr(chat_store, "list_sessions",
                        lambda *a, **k: [{"id": 7, "cwd": None}])
    monkeypatch.setattr(chat_store, "get_messages", lambda sid, *a, **k: [{"x": 1}])
    monkeypatch.setattr(md_store, "compact",
                        lambda *a, **k: calls.append(("compact", k.get("group_by"))) or {})
    monkeypatch.setattr(md_store, "sweep_stale_captures", lambda *a, **k: {})
    monkeypatch.setattr(md_store, "sweep_empty_briefs", lambda *a, **k: {})
    monkeypatch.setattr(md_store, "finalize_briefs", lambda *a, **k: {})
    monkeypatch.setattr(migrations, "force_recompact_all",
                        lambda *a, **k: calls.append(("recompact", None)) or {})
    monkeypatch.setattr(chat_okr, "compact_session",
                        lambda *a, **k: calls.append(("session", None)) or {"ok": True})

    tasks = _registered(monkeypatch, tmp_path)
    tasks["daily-compact"].fn()
    assert calls == [("session", None), ("compact", "repo"), ("compact", "topic"),
                     ("recompact", None)]


def test_daily_pass_folds_every_session_with_new_turns(monkeypatch, tmp_path):
    """Not just idle ones — the two-scan idle handshake can't work once a day.

    compact_session is offset-based, so a session still in flight loses nothing.
    """
    folded: list = []
    import aiforge_core.runtime.chat_okr as chat_okr
    import aiforge_core.runtime.chat_store as chat_store
    from aiforge_core.memory import md_store, migrations
    monkeypatch.setattr(chat_store, "list_sessions",
                        lambda *a, **k: [{"id": 1, "cwd": None}, {"id": 2, "cwd": None}])
    monkeypatch.setattr(chat_store, "get_messages", lambda sid, *a, **k: [{"x": 1}] * 3)
    monkeypatch.setattr(chat_okr, "compact_session",
                        lambda sid, **k: folded.append(sid) or {"ok": True,
                                                                "remaining": 0})
    for name in ("compact", "sweep_stale_captures", "sweep_empty_briefs",
                 "finalize_briefs"):
        monkeypatch.setattr(md_store, name, lambda *a, **k: {})
    monkeypatch.setattr(migrations, "force_recompact_all", lambda *a, **k: {})

    tasks = _registered(monkeypatch, tmp_path)
    tasks["daily-compact"].fn()
    assert sorted(folded) == [1, 2]          # first pass folds both, no idle wait
    folded.clear()
    tasks["daily-compact"].fn()              # same message count → nothing new
    assert folded == []


def test_a_failing_stage_does_not_cancel_the_others(monkeypatch, tmp_path):
    """As three registered tasks one could not kill the others; folded into one
    pass they still must not."""
    ran: list = []
    import aiforge_core.runtime.chat_store as chat_store
    from aiforge_core.memory import md_store, migrations

    def _boom(*a, **k):
        raise OSError("stage 1 is broken")

    monkeypatch.setattr(chat_store, "list_sessions", _boom)
    monkeypatch.setattr(md_store, "compact",
                        lambda *a, **k: ran.append("briefs") or {})
    for name in ("sweep_stale_captures", "sweep_empty_briefs", "finalize_briefs"):
        monkeypatch.setattr(md_store, name, lambda *a, **k: {})
    monkeypatch.setattr(migrations, "force_recompact_all",
                        lambda *a, **k: ran.append("recompact") or {})

    tasks = _registered(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError):        # the pass reports itself failed…
        tasks["daily-compact"].fn()
    assert "briefs" in ran and "recompact" in ran   # …but stages 2-3 still ran


def test_session_stage_failure_is_reported_but_contained(monkeypatch, tmp_path):
    import aiforge_core.runtime.chat_okr as chat_okr
    import aiforge_core.runtime.chat_store as chat_store
    from aiforge_core.memory import md_store, migrations
    monkeypatch.setattr(chat_store, "list_sessions",
                        lambda *a, **k: [{"id": 1, "cwd": None}])
    monkeypatch.setattr(chat_store, "get_messages", lambda sid, *a, **k: [{"x": 1}])
    monkeypatch.setattr(chat_okr, "compact_session",
                        lambda *a, **k: {"ok": True, "remaining": 0})
    for name in ("compact", "sweep_stale_captures", "sweep_empty_briefs",
                 "finalize_briefs"):
        monkeypatch.setattr(md_store, name, lambda *a, **k: {})
    monkeypatch.setattr(migrations, "force_recompact_all", lambda *a, **k: {})
    tasks = _registered(monkeypatch, tmp_path)
    tasks["daily-compact"].fn()              # a clean pass does not raise


def test_session_walk_stops_when_the_model_is_down(monkeypatch, tmp_path):
    """extract_failed leaves the turns unfolded — retrying them in a tight loop
    would just burn the whole window on a dead provider."""
    import aiforge_core.runtime.chat_okr as chat_okr
    import aiforge_core.runtime.chat_store as chat_store
    from aiforge_core.memory import md_store, migrations
    folds: list = []
    monkeypatch.setattr(chat_store, "list_sessions",
                        lambda *a, **k: [{"id": 1, "cwd": None}])
    monkeypatch.setattr(chat_store, "get_messages", lambda sid, *a, **k: [{"x": 1}] * 5)
    monkeypatch.setattr(chat_okr, "compact_session", lambda *a, **k: folds.append(1) or {
        "ok": True, "skipped": "extract_failed", "remaining": 5})
    for name in ("compact", "sweep_stale_captures", "sweep_empty_briefs",
                 "finalize_briefs"):
        monkeypatch.setattr(md_store, name, lambda *a, **k: {})
    monkeypatch.setattr(migrations, "force_recompact_all", lambda *a, **k: {})
    tasks = _registered(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError):        # an outage is NOT a successful pass
        tasks["daily-compact"].fn()
    assert len(folds) == 1                   # and it doesn't hammer the dead model


def test_a_session_whose_walk_stopped_short_is_revisited_next_pass(monkeypatch,
                                                                   tmp_path):
    """Stopping at the window cap must not mark the session done — with a
    "has the count changed" due test, yesterday's finished chat would never be
    revisited and its tail would be folded by nobody, ever."""
    monkeypatch.setenv("AIFORGE_SESSION_COMPACT_MAX_WINDOWS", "3")
    import aiforge_core.runtime.chat_okr as chat_okr
    import aiforge_core.runtime.chat_store as chat_store
    from aiforge_core.memory import md_store, migrations
    folds: list = []
    monkeypatch.setattr(chat_store, "list_sessions",
                        lambda *a, **k: [{"id": 1, "cwd": None}])
    monkeypatch.setattr(chat_store, "get_messages", lambda sid, *a, **k: [{"x": 1}] * 9)
    monkeypatch.setattr(chat_okr, "compact_session", lambda *a, **k: folds.append(1) or {
        "ok": True, "remaining": 99})            # always more to fold
    for name in ("compact", "sweep_stale_captures", "sweep_empty_briefs",
                 "finalize_briefs"):
        monkeypatch.setattr(md_store, name, lambda *a, **k: {})
    monkeypatch.setattr(migrations, "force_recompact_all", lambda *a, **k: {})

    tasks = _registered(monkeypatch, tmp_path,
                        AIFORGE_SESSION_COMPACT_MAX_WINDOWS="3")
    import aiforge_core.api.api as api
    day = [date(2026, 8, 19)]
    monkeypatch.setattr(api, "_date", type("D", (), {"today": staticmethod(
        lambda: day[0])}))
    tasks["daily-compact"].fn()
    assert len(folds) == 3                       # the cap holds…
    day[0] = date(2026, 8, 20)                   # next day's pass
    tasks["daily-compact"].fn()
    assert len(folds) == 6                       # …and the backlog is picked up again


def test_a_drained_session_is_not_refolded_next_pass(monkeypatch, tmp_path):
    import aiforge_core.runtime.chat_okr as chat_okr
    import aiforge_core.runtime.chat_store as chat_store
    from aiforge_core.memory import md_store, migrations
    folds: list = []
    monkeypatch.setattr(chat_store, "list_sessions",
                        lambda *a, **k: [{"id": 1, "cwd": None}])
    monkeypatch.setattr(chat_store, "get_messages", lambda sid, *a, **k: [{"x": 1}] * 4)
    monkeypatch.setattr(chat_okr, "compact_session", lambda *a, **k: folds.append(1) or {
        "ok": True, "remaining": 0})
    for name in ("compact", "sweep_stale_captures", "sweep_empty_briefs",
                 "finalize_briefs"):
        monkeypatch.setattr(md_store, name, lambda *a, **k: {})
    monkeypatch.setattr(migrations, "force_recompact_all", lambda *a, **k: {})
    tasks = _registered(monkeypatch, tmp_path)
    import aiforge_core.api.api as api
    day = [date(2026, 8, 19)]
    monkeypatch.setattr(api, "_date", type("D", (), {"today": staticmethod(
        lambda: day[0])}))
    tasks["daily-compact"].fn()
    day[0] = date(2026, 8, 20)
    tasks["daily-compact"].fn()
    assert len(folds) == 1


def test_a_raising_session_fold_fails_the_pass_but_not_the_other_stages(
        monkeypatch, tmp_path):
    ran: list = []
    import aiforge_core.runtime.chat_okr as chat_okr
    import aiforge_core.runtime.chat_store as chat_store
    from aiforge_core.memory import md_store, migrations

    def _boom(*a, **k):
        raise RuntimeError("fold blew up")

    monkeypatch.setattr(chat_store, "list_sessions",
                        lambda *a, **k: [{"id": 1, "cwd": None}])
    monkeypatch.setattr(chat_store, "get_messages", lambda sid, *a, **k: [{"x": 1}])
    monkeypatch.setattr(chat_okr, "compact_session", _boom)
    monkeypatch.setattr(md_store, "compact", lambda *a, **k: ran.append("briefs") or {})
    for name in ("sweep_stale_captures", "sweep_empty_briefs", "finalize_briefs"):
        monkeypatch.setattr(md_store, name, lambda *a, **k: {})
    monkeypatch.setattr(migrations, "force_recompact_all",
                        lambda *a, **k: ran.append("recompact") or {})

    tasks = _registered(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError):        # the pass reports itself failed…
        tasks["daily-compact"].fn()
    assert "briefs" in ran and "recompact" in ran   # …and stages 2-3 still ran


def test_a_retry_does_not_re_run_the_stages_that_already_succeeded(monkeypatch,
                                                                   tmp_path):
    """The pass raises so the scheduler retries — but one broken session fold
    must not buy a second full recompact."""
    ran: list = []
    import aiforge_core.runtime.chat_okr as chat_okr
    import aiforge_core.runtime.chat_store as chat_store
    from aiforge_core.memory import md_store, migrations

    def _boom(*a, **k):
        raise RuntimeError("fold blew up")

    monkeypatch.setattr(chat_store, "list_sessions",
                        lambda *a, **k: [{"id": 1, "cwd": None}])
    monkeypatch.setattr(chat_store, "get_messages", lambda sid, *a, **k: [{"x": 1}])
    monkeypatch.setattr(chat_okr, "compact_session", _boom)
    monkeypatch.setattr(md_store, "compact", lambda *a, **k: ran.append("briefs") or {})
    for name in ("sweep_stale_captures", "sweep_empty_briefs", "finalize_briefs"):
        monkeypatch.setattr(md_store, name, lambda *a, **k: {})
    monkeypatch.setattr(migrations, "force_recompact_all",
                        lambda *a, **k: ran.append("recompact") or {})

    tasks = _registered(monkeypatch, tmp_path)
    for _ in range(3):                       # the pass plus two retries
        with pytest.raises(RuntimeError):
            tasks["daily-compact"].fn()
    assert ran.count("recompact") == 1       # heavy stages ran ONCE, not 3x
    assert ran.count("briefs") == 2          # md_store.compact = repo + topic axis


def test_the_pass_fails_when_the_learner_is_down(monkeypatch, tmp_path):
    """A provider outage is the most likely real failure and it does NOT raise
    out of compact_session — so the stage has to notice, or the whole retry
    budget (_MAX_FAILS / _RETRY_S / _hold) never engages."""
    import aiforge_core.runtime.chat_okr as chat_okr
    import aiforge_core.runtime.chat_store as chat_store
    from aiforge_core.memory import md_store, migrations
    monkeypatch.setattr(chat_store, "list_sessions",
                        lambda *a, **k: [{"id": 1, "cwd": None}])
    monkeypatch.setattr(chat_store, "get_messages", lambda sid, *a, **k: [{"x": 1}] * 4)
    monkeypatch.setattr(chat_okr, "compact_session", lambda *a, **k: {
        "ok": True, "skipped": "extract_failed", "captured": 0, "remaining": 4})
    for name in ("compact", "sweep_stale_captures", "sweep_empty_briefs",
                 "finalize_briefs"):
        monkeypatch.setattr(md_store, name, lambda *a, **k: {})
    monkeypatch.setattr(migrations, "force_recompact_all", lambda *a, **k: {})
    tasks = _registered(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError):
        tasks["daily-compact"].fn()


def test_a_drained_session_reports_no_new_and_is_not_reprobed(monkeypatch,
                                                              tmp_path):
    """After a restart every session is "due" again; the first call returns
    no_new, which must count as DRAINED — otherwise every session is re-probed
    on every pass, forever."""
    import aiforge_core.runtime.chat_okr as chat_okr
    import aiforge_core.runtime.chat_store as chat_store
    from aiforge_core.memory import md_store, migrations
    folds: list = []
    monkeypatch.setattr(chat_store, "list_sessions",
                        lambda *a, **k: [{"id": 1, "cwd": None, "created_at": "t0"}])
    monkeypatch.setattr(chat_store, "get_messages", lambda sid, *a, **k: [{"x": 1}] * 4)
    monkeypatch.setattr(chat_okr, "compact_session", lambda *a, **k: folds.append(1) or {
        "ok": True, "skipped": "no_new", "captured": 0})
    for name in ("compact", "sweep_stale_captures", "sweep_empty_briefs",
                 "finalize_briefs"):
        monkeypatch.setattr(md_store, name, lambda *a, **k: {})
    monkeypatch.setattr(migrations, "force_recompact_all", lambda *a, **k: {})
    tasks = _registered(monkeypatch, tmp_path)
    import aiforge_core.api.api as api
    day = [date(2026, 8, 19)]
    monkeypatch.setattr(api, "_date", type("D", (), {"today": staticmethod(
        lambda: day[0])}))
    tasks["daily-compact"].fn()
    day[0] = date(2026, 8, 20)
    tasks["daily-compact"].fn()
    assert len(folds) == 1


def test_a_reused_session_id_is_not_suppressed_by_stale_scan_state(monkeypatch,
                                                                   tmp_path):
    """Session ids restart at 1 after a bulk reset; a leftover done=True entry
    would silence the new chat's fold."""
    import aiforge_core.runtime.chat_okr as chat_okr
    import aiforge_core.runtime.chat_store as chat_store
    from aiforge_core.memory import md_store, migrations
    folds: list = []
    sess = [{"id": 1, "cwd": None, "created_at": "t0"}]
    monkeypatch.setattr(chat_store, "list_sessions", lambda *a, **k: sess)
    monkeypatch.setattr(chat_store, "get_messages", lambda sid, *a, **k: [{"x": 1}] * 4)
    monkeypatch.setattr(chat_okr, "compact_session", lambda *a, **k: folds.append(1) or {
        "ok": True, "remaining": 0, "captured": 1})
    for name in ("compact", "sweep_stale_captures", "sweep_empty_briefs",
                 "finalize_briefs"):
        monkeypatch.setattr(md_store, name, lambda *a, **k: {})
    monkeypatch.setattr(migrations, "force_recompact_all", lambda *a, **k: {})
    tasks = _registered(monkeypatch, tmp_path)
    import aiforge_core.api.api as api
    day = [date(2026, 8, 19)]
    monkeypatch.setattr(api, "_date", type("D", (), {"today": staticmethod(
        lambda: day[0])}))
    tasks["daily-compact"].fn()
    assert len(folds) == 1
    sess[0] = {"id": 1, "cwd": None, "created_at": "t1"}    # same id, NEW chat
    day[0] = date(2026, 8, 20)
    tasks["daily-compact"].fn()
    assert len(folds) == 2


def test_session_folding_is_not_silently_off_for_other_trigger_modes(monkeypatch,
                                                                     tmp_path):
    """AIFORGE_SESSION_COMPACT picks the DAEMON trigger; with the daily pass on
    there is no daemon, so anything but 'off' must still fold."""
    import aiforge_core.runtime.chat_okr as chat_okr
    import aiforge_core.runtime.chat_store as chat_store
    from aiforge_core.memory import md_store, migrations
    folds: list = []
    monkeypatch.setattr(chat_store, "list_sessions",
                        lambda *a, **k: [{"id": 1, "cwd": None}])
    monkeypatch.setattr(chat_store, "get_messages", lambda sid, *a, **k: [{"x": 1}] * 4)
    monkeypatch.setattr(chat_okr, "compact_session",
                        lambda *a, **k: folds.append(1) or {"ok": True, "remaining": 0})
    for name in ("compact", "sweep_stale_captures", "sweep_empty_briefs",
                 "finalize_briefs"):
        monkeypatch.setattr(md_store, name, lambda *a, **k: {})
    monkeypatch.setattr(migrations, "force_recompact_all", lambda *a, **k: {})
    tasks = _registered(monkeypatch, tmp_path, AIFORGE_SESSION_COMPACT="explicit")
    tasks["daily-compact"].fn()
    assert folds == [1]
    folds.clear()
    tasks2 = _registered(monkeypatch, tmp_path, AIFORGE_SESSION_COMPACT="off")
    tasks2["daily-compact"].fn()
    assert folds == []                        # 'off' still means off


def test_api_delegates_the_hour_parse_to_compact_window(monkeypatch):
    """One parser, not two — the scheduled pass and the opportunistic chat
    folds must not disagree about when the window opens."""
    from aiforge_core.api import api
    from aiforge_core.runtime import compact_window
    for raw in ("18", "off", "0", "24", "99", "nonsense"):
        monkeypatch.setenv("AIFORGE_COMPACT_AT_HOUR", raw)
        assert api._compact_at_hour() == compact_window.at_hour()
    # The branch that could actually diverge: hour unset, hourly interval set.
    monkeypatch.delenv("AIFORGE_COMPACT_AT_HOUR")
    monkeypatch.setenv("AIFORGE_COMPACT_EVERY_H", "2")
    assert api._compact_at_hour() is None
    assert compact_window.at_hour() is None
