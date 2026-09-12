"""Memory compaction whenever AIForge is idle — yielding to the user and
resuming where it stopped (runtime.compact_idle + the resumable compact)."""
import time

import pytest

from aiforge_core.runtime import compact_idle as ci


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(ci, "_tickets_in_progress", lambda: False)
    from aiforge_core.runtime import chat_runs
    monkeypatch.setattr(chat_runs, "_LAST_ACTIVITY", [0.0])
    monkeypatch.setattr(chat_runs, "_RUNS", {})


@pytest.fixture
def activity(monkeypatch):
    state = {"active": False}
    monkeypatch.setattr(ci, "user_active", lambda: state["active"])
    return state


def test_idle_means_no_run_no_recent_chat_no_ticket(monkeypatch):
    from aiforge_core.runtime import chat_runs
    assert ci.user_active() is False
    chat_runs._touch()                                  # chat just now
    assert ci.user_active() is True
    monkeypatch.setattr(chat_runs, "_LAST_ACTIVITY", [time.time() - 3600])
    assert ci.user_active() is False
    monkeypatch.setattr(ci, "_tickets_in_progress", lambda: True)
    assert ci.user_active() is True


def test_nothing_runs_while_someone_is_working(activity):
    activity["active"] = True
    assert ci.run_when_idle([("a", lambda cp: pytest.fail("ran while busy"))]) == "busy"


def test_a_cycle_runs_its_stages_in_order_and_completes(activity):
    ran = []
    out = ci.run_when_idle([(n, lambda cp, n=n: ran.append(n) or "done")
                            for n in ("sessions", "briefs", "recompact")])
    assert out == "done"
    assert ran == ["sessions", "briefs", "recompact"]
    # the next idle window finds nothing due until the cycle interval passes
    assert ci.run_when_idle([("sessions", lambda cp: pytest.fail("re-ran"))]) == "not-due"


def test_a_stopped_cycle_resumes_where_it_stopped(activity):
    ran = []

    def briefs(cp):
        ran.append("briefs")
        return "stopped" if ran.count("briefs") == 1 else "done"

    stages = [("sessions", lambda cp: ran.append("sessions") or "done"),
              ("briefs", briefs),
              ("recompact", lambda cp: ran.append("recompact") or "done")]
    assert ci.run_when_idle(stages) == "stopped"
    assert ci.run_when_idle(stages) == "done"          # next idle window
    assert ran == ["sessions", "briefs", "briefs", "recompact"]   # sessions not redone


def test_progress_survives_a_restart(activity):
    cp = ci.Checkpoint.load()
    cp.begin()
    cp.group_done("topic", "gps")
    again = ci.Checkpoint.load()                        # a new process
    assert again.groups_done("topic") == {"gps"}
    assert again.due() is True                          # unfinished → resume


def test_a_stage_that_keeps_failing_is_skipped_after_three(activity):
    calls = []
    stages = [("briefs", lambda cp: calls.append(1) or "failed"),
              ("recompact", lambda cp: "done")]
    assert ci.run_when_idle(stages) == "failed"
    assert ci.run_when_idle(stages) == "failed"
    assert ci.run_when_idle(stages) == "done"           # third failure → skipped
    assert len(calls) == 3


# ── the resumable compact ────────────────────────────────────────────────

def test_compact_skips_done_groups_writes_each_and_stops_when_asked(monkeypatch):
    from aiforge_core.memory.md_store import _compact as c
    planned = {"a": [{"file": "1"}], "b": [{"file": "2"}], "c": [{"file": "3"}]}
    monkeypatch.setattr(c, "_gather_planned", lambda *a: planned)
    monkeypatch.setattr(c, "_prepare_group", lambda key, items, **k: {"key": key,
                                                                      "items": items, "parts": []})
    written, done = [], []
    monkeypatch.setattr(c, "_write_prepared",
                        lambda prepared, *a: written.extend(p["key"] for p in prepared) or 0)
    monkeypatch.setattr(c, "_reingest_prepared", lambda *a: None)
    monkeypatch.setattr(c, "_heal_after_compact", lambda *a: ({}, {}))
    def should_stop():
        return len(done) >= 1                          # user back after one group
    out = c.compact(group_by="repo", force=True, skip_keys={"a"},
                    should_stop=should_stop, on_group_done=done.append)
    assert done == ["b"]                               # "a" skipped, stopped before "c"
    assert written == ["b"]                            # written as soon as folded
    assert out["stopped"] is True


def test_the_full_refold_resumes_steps_and_stops_between_them(monkeypatch):
    from aiforge_core.memory import md_store, migrations
    ran = []
    for fn in ("cleanup_legacy_compacted", "sweep_stale_captures", "sweep_empty_briefs",
               "fold_kind_briefs", "merge_similar_topics", "dedupe_global_copies",
               "reconcile_briefs", "resolve_contradictions", "lint_graph", "map_scopes",
               "ingest_dir"):
        monkeypatch.setattr(md_store, fn, lambda *a, _n=fn, **k: ran.append(_n) or {"ok": True})
    monkeypatch.setattr(md_store, "compact",
                        lambda **k: ran.append("compact-" + k["group_by"]) or {"ok": True})
    monkeypatch.setattr(migrations, "dedupe_all", lambda: ran.append("dedupe") or {})

    class _CP:
        def __init__(self):
            self.steps, self.stop = {"tidy_legacy", "repo"}, False
        def step_done_already(self, n):
            return n in self.steps
        def step_done(self, n):
            self.steps.add(n)
            if n == "topic":
                self.stop = True                       # user comes back
        def should_stop(self):
            return self.stop
        def groups_done(self, axis):
            return set()
        def group_done(self, axis, key):
            pass
    cp = _CP()
    out = migrations.force_recompact_all(checkpoint=cp)
    assert out["stopped"] is True
    assert ran == ["compact-topic"]                    # done steps skipped, then paused
