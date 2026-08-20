"""The scheduled-jobs daemon must actually be registered.

On 2026-07-07 a refactor inserted `_check_tool_parity` directly beneath
`_start_jobs_scheduler` and took its `@app.on_event("startup")` with it,
leaving that decorator written TWICE on the new function and none on this one.
Every scheduled job since sat in the table with a next_run_at nothing
advanced: rows written, tickets never filed, no error anywhere — the failure
mode nothing notices. Only POST /api/jobs/{id}/run-now did anything.

Nothing else in the suite would catch it happening again.
"""
import importlib


def test_the_jobs_scheduler_is_registered_at_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    import aiforge_core.api.api as api
    importlib.reload(api)
    names = [h.__name__ for h in api.app.router.on_startup]
    assert "_start_jobs_scheduler" in names, (
        "the jobs scheduler is not started — scheduled tasks will never fire")
    # And exactly once: a duplicate registration is what stole it last time.
    for name in set(names):
        assert names.count(name) == 1, f"{name} registered {names.count(name)}x"
