"""Keeping the codegraph index honest.

The index is a SQLite store built by an external binary, and every rule here
comes from a way that went wrong:

  * a build that timed out or aborted leaves an EMPTY or partial ``.codegraph``
    directory, and trusting mere existence made every later turn short-circuit
    onto a corrupt index forever — so "indexed" means POPULATED;
  * "no DB file found" is not "corrupt": the binary may name its store with an
    extension we cannot probe, and deleting those builds destroyed good work;
  * a TIMEOUT means the process did not exit in time, not that the DB is
    incomplete — a build that finished writing and overran on teardown is kept;
  * a repo that cannot index inside the budget must not re-trigger a blocking
    build every turn, so a failure starts a cooldown;
  * the build lock lives OUTSIDE the repo, keyed by the canonical path, so it
    can never be staged into the Doer's PR and so two spellings of one repo
    (a symlink, the "." fallback) still exclude each other.

A Doer runs inside ``<repo>/.aiforge-worktrees/<TICKET>``, which has no index
of its own — queries resolve back to the parent repo or they hit nothing.
"""
from __future__ import annotations

import os
import sqlite3
import types as pytypes

import pytest

from aiforge_core.runtime.tools import codegraph as C


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.delenv("AIFORGE_CODEGRAPH_DISABLE", raising=False)
    monkeypatch.delenv("AIFORGE_CODEGRAPH_PATH", raising=False)
    monkeypatch.delenv("AIFORGE_CURRENT_TICKET", raising=False)
    C._FAILED.clear()
    C._VERIFIED_HEALTHY.clear()
    yield
    C._FAILED.clear()
    C._VERIFIED_HEALTHY.clear()


def _index(root, *, populated=True, db=None):
    d = root / ".codegraph"
    d.mkdir(exist_ok=True)
    if populated and db is None:
        (d / "graph.json").write_text("{}")
    if db is not None:
        p = d / "index.db"
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE t (x int)")
        con.commit()
        con.close()
        if db == "corrupt":
            p.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    return d


# ─── finding the binary and the repo ───────────────────────────────────


def test_an_explicit_binary_path_wins(monkeypatch, tmp_path):
    exe = tmp_path / "codegraph"
    exe.write_text("#!/bin/sh")
    monkeypatch.setenv("AIFORGE_CODEGRAPH_BIN", str(exe))
    assert C._bin() == str(exe)
    assert C.available() is True


def test_a_binary_path_that_does_not_exist_falls_back(monkeypatch):
    monkeypatch.setenv("AIFORGE_CODEGRAPH_BIN", "/nope/codegraph")
    monkeypatch.setattr(C.shutil, "which", lambda n: "/usr/local/bin/codegraph")
    assert C._bin() == "/usr/local/bin/codegraph"


def test_no_binary_anywhere_means_unavailable(monkeypatch):
    monkeypatch.delenv("AIFORGE_CODEGRAPH_BIN", raising=False)
    monkeypatch.setattr(C.shutil, "which", lambda n: None)
    monkeypatch.setattr(C.os.path, "exists", lambda p: False)
    assert C.available() is False


@pytest.mark.parametrize("stem", [".aiforge-worktrees", ".worktrees"])
def test_a_doers_worktree_resolves_back_to_the_parent_repo(stem):
    """The worktree has no index of its own."""
    assert C._main_repo(f"/repos/app/{stem}/ONE-7") == "/repos/app"


def test_a_plain_repo_path_is_left_alone():
    assert C._main_repo("/repos/app") == "/repos/app"


def test_the_env_pins_the_repo(monkeypatch):
    monkeypatch.setenv("AIFORGE_CODEGRAPH_PATH", "/repos/app/.worktrees/X")
    assert C._repo("/elsewhere") == "/repos/app"


def test_the_request_context_names_the_repo(monkeypatch):
    from aiforge_core.runtime import request_context
    monkeypatch.setattr(request_context, "get_repo_root", lambda: "/repos/app")
    assert C._repo(None) == "/repos/app"


def test_without_any_context_the_cwd_is_used(monkeypatch):
    from aiforge_core.runtime import request_context
    monkeypatch.setattr(request_context, "get_repo_root",
                        lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert C._repo("/here") == "/here"


# ─── is there a usable index ───────────────────────────────────────────


def test_a_populated_index_counts(tmp_path, monkeypatch):
    _index(tmp_path)
    monkeypatch.setenv("AIFORGE_CODEGRAPH_PATH", str(tmp_path))
    assert C.indexed() is True


def test_an_empty_directory_left_by_an_aborted_build_does_not(tmp_path,
                                                               monkeypatch):
    """Trusting mere existence short-circuited every later turn onto it."""
    _index(tmp_path, populated=False)
    monkeypatch.setenv("AIFORGE_CODEGRAPH_PATH", str(tmp_path))
    assert C.indexed() is False


def test_no_directory_at_all_is_not_indexed(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CODEGRAPH_PATH", str(tmp_path))
    assert C.indexed() is False


def test_an_unreadable_repo_is_not_indexed(monkeypatch):
    monkeypatch.setattr(C, "_repo",
                        lambda cwd: (_ for _ in ()).throw(OSError("x")))
    assert C.indexed() is False


# ─── integrity ─────────────────────────────────────────────────────────


def test_sidecar_files_are_not_mistaken_for_the_database(tmp_path):
    d = _index(tmp_path, db="ok")
    for name in ("index.db-wal", "index.db-shm", "index.db-journal"):
        (d / name).write_bytes(b"")
    assert [os.path.basename(p) for p in C._db_files(str(tmp_path))] == \
        ["index.db"]


def test_a_healthy_database_is_not_corrupt(tmp_path):
    _index(tmp_path, db="ok")
    assert C._db_corrupt(str(tmp_path)) is False
    assert C._index_healthy(str(tmp_path)) is True


def test_a_torn_database_is_proven_corrupt(tmp_path):
    _index(tmp_path, db="corrupt")
    assert C._db_corrupt(str(tmp_path)) is True


def test_a_store_we_cannot_recognise_is_not_called_corrupt(tmp_path):
    """Deleting those deleted good builds."""
    _index(tmp_path)                      # only graph.json
    assert C._db_corrupt(str(tmp_path)) is False
    assert C._index_healthy(str(tmp_path)) is False, "but not provably usable"


def test_a_stub_index_is_removed(tmp_path):
    _index(tmp_path)
    C._VERIFIED_HEALTHY.add(str(tmp_path))
    C._remove_partial_index(str(tmp_path))
    assert not (tmp_path / ".codegraph").exists()
    assert str(tmp_path) not in C._VERIFIED_HEALTHY


# ─── the build lock ────────────────────────────────────────────────────


def test_the_lock_lives_outside_the_repo(tmp_path):
    """A <repo>/.codegraph.build.lock would be staged into the Doer's PR."""
    path = C._build_lock_path(str(tmp_path))
    assert not path.startswith(str(tmp_path))
    assert "aiforge-codegraph-" in os.path.basename(path)


def test_two_spellings_of_one_repo_share_a_lock(tmp_path):
    link = tmp_path / "link"
    real = tmp_path / "real"
    real.mkdir()
    link.symlink_to(real)
    assert C._build_lock_path(str(link)) == C._build_lock_path(str(real))
    assert C._canon_repo(str(link)) == C._canon_repo(str(real))


def test_an_unresolvable_path_still_produces_a_lock(monkeypatch):
    monkeypatch.setattr(C.os.path, "realpath",
                        lambda p: (_ for _ in ()).throw(OSError("x")))
    assert C._build_lock_path("/repos/app")
    assert C._canon_repo("/repos/app") == "/repos/app"


def test_the_first_process_takes_the_lock_and_the_second_is_told_no(tmp_path):
    first = C._acquire_build_lock(str(tmp_path))
    try:
        assert not isinstance(first, str), "a real flock"
        assert C._acquire_build_lock(str(tmp_path)) is None
    finally:
        first.close()


def test_a_platform_without_flock_proceeds_on_the_thread_lock(monkeypatch,
                                                              tmp_path):
    import builtins
    real = builtins.__import__

    def _imp(name, *a, **k):
        if name == "fcntl":
            raise ImportError("windows")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _imp)
    assert C._acquire_build_lock(str(tmp_path)) == "nolock"


def test_an_unwritable_lock_file_does_not_block_the_build(monkeypatch,
                                                          tmp_path):
    monkeypatch.setattr(C, "_build_lock_path", lambda r: "/nope/dir/x.lock")
    assert C._acquire_build_lock(str(tmp_path)) == "nolock"


# ─── the gate ──────────────────────────────────────────────────────────


@pytest.fixture
def gate(monkeypatch):
    state: dict = {"available": True, "indexed": True}
    monkeypatch.setattr(C, "available", lambda: state["available"])
    monkeypatch.setattr(C, "indexed", lambda cwd=None: state["indexed"])
    return state


def test_the_tools_are_offered_when_binary_and_index_are_both_there(gate):
    assert C.enabled_for_run("/repo") is True


@pytest.mark.parametrize("flag", ["available", "indexed"])
def test_either_half_missing_turns_them_off(gate, flag):
    gate[flag] = False
    assert C.enabled_for_run("/repo") is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_codegraph_can_be_switched_off_entirely(gate, monkeypatch, val):
    monkeypatch.setenv("AIFORGE_CODEGRAPH_DISABLE", val)
    assert C.enabled_for_run("/repo") is False


@pytest.mark.parametrize("val", [False, 0, "false", "0", "off", "no"])
def test_one_ticket_can_opt_out(gate, monkeypatch, val):
    from aiforge_core.tickets import store
    monkeypatch.setenv("AIFORGE_CURRENT_TICKET", "ONE-7")
    monkeypatch.setattr(store, "get",
                        lambda ident: pytypes.SimpleNamespace(
                            metadata={"codegraph": val}))
    assert C.enabled_for_run("/repo") is False


def test_a_ticket_that_says_nothing_keeps_it_on(gate, monkeypatch):
    from aiforge_core.tickets import store
    monkeypatch.setenv("AIFORGE_CURRENT_TICKET", "ONE-7")
    monkeypatch.setattr(store, "get",
                        lambda ident: pytypes.SimpleNamespace(metadata={}))
    assert C.enabled_for_run("/repo") is True


def test_a_ticket_store_hiccup_never_gates_the_run(gate, monkeypatch):
    from aiforge_core.tickets import store
    monkeypatch.setenv("AIFORGE_CURRENT_TICKET", "ONE-7")
    monkeypatch.setattr(store, "get",
                        lambda ident: (_ for _ in ()).throw(OSError("db")))
    assert C.enabled_for_run("/repo") is True


# ─── building it ───────────────────────────────────────────────────────


@pytest.fixture
def build(monkeypatch, tmp_path):
    """A repo with no index, a fake binary, and a scripted init."""
    monkeypatch.setenv("AIFORGE_CODEGRAPH_PATH", str(tmp_path))
    state: dict = {"rc": 0, "raise": None, "calls": [], "root": tmp_path,
                   "builds_index": True}
    monkeypatch.setattr(C, "_bin", lambda: "/usr/bin/codegraph")

    def _init(exe, repo, timeout_s):
        state["calls"].append((exe, repo, timeout_s))
        if state["raise"]:
            raise state["raise"]
        if state["builds_index"]:
            _index(tmp_path)
        return pytypes.SimpleNamespace(returncode=state["rc"], stdout="",
                                       stderr="")
    monkeypatch.setattr(C, "_run_init", _init)
    return state


def test_a_missing_index_is_built_once(build, tmp_path):
    assert C.ensure_indexed(str(tmp_path)) is True
    assert len(build["calls"]) == 1
    assert (tmp_path / ".codegraph").is_dir()


def test_an_existing_healthy_index_is_not_rebuilt(build, tmp_path):
    _index(tmp_path, db="ok")
    assert C.ensure_indexed(str(tmp_path)) is True
    assert build["calls"] == []


def test_the_health_check_runs_once_per_repo(build, tmp_path, monkeypatch):
    _index(tmp_path, db="ok")
    C.ensure_indexed(str(tmp_path))
    monkeypatch.setattr(C, "_db_corrupt",
                        lambda r: pytest.fail("re-verified a known-good index"))
    assert C.ensure_indexed(str(tmp_path)) is True


def test_a_crash_leftover_is_rebuilt_rather_than_trusted_forever(build,
                                                                 tmp_path):
    """A corrupt index from an OOM-killed prior process had no cleanup."""
    _index(tmp_path, db="corrupt")
    assert C.ensure_indexed(str(tmp_path)) is True
    assert len(build["calls"]) == 1


def test_a_failed_build_starts_a_cooldown(build, tmp_path):
    build["rc"] = 1
    build["builds_index"] = False
    assert C.ensure_indexed(str(tmp_path)) is False
    assert C.ensure_indexed(str(tmp_path)) is False
    assert len(build["calls"]) == 1, "the blocking build is not retried"


def test_the_cooldown_is_tunable(monkeypatch):
    monkeypatch.setenv("AIFORGE_CODEGRAPH_RETRY_COOLDOWN_S", "120")
    assert C._retry_cooldown_s() == 120
    monkeypatch.setenv("AIFORGE_CODEGRAPH_RETRY_COOLDOWN_S", "5")
    assert C._retry_cooldown_s() == 60, "with a floor"
    monkeypatch.setenv("AIFORGE_CODEGRAPH_RETRY_COOLDOWN_S", "soon")
    assert C._retry_cooldown_s() == 3600


def test_a_build_that_overran_but_wrote_a_good_db_is_kept(build, tmp_path):
    """A timeout means the PROCESS did not exit in time, not that the DB is
    incomplete."""
    _index(tmp_path, db="ok")
    assert C._after_timeout(str(tmp_path), str(tmp_path), True) is True
    assert (tmp_path / ".codegraph").is_dir()
    assert str(tmp_path) in C._VERIFIED_HEALTHY


def test_a_timeout_that_left_a_corrupt_db_is_thrown_away(build, tmp_path):
    _index(tmp_path, db="corrupt")
    assert C._after_timeout(str(tmp_path), str(tmp_path), True) is False
    assert not (tmp_path / ".codegraph").exists()


def test_a_timed_out_build_takes_the_timeout_path(build, tmp_path):
    build["raise"] = C.subprocess.TimeoutExpired("codegraph", 180)
    build["builds_index"] = False
    assert C.ensure_indexed(str(tmp_path)) is False
    assert C._canon_repo(str(tmp_path)) in C._FAILED


def test_an_unprobeable_index_is_left_alone_without_the_real_lock(build,
                                                                  tmp_path):
    _index(tmp_path)                      # no DB we can read
    assert C._mark_failed(str(tmp_path), have_lock=False) is False
    assert (tmp_path / ".codegraph").is_dir()


def test_holding_the_real_lock_lets_a_stub_be_removed(build, tmp_path):
    _index(tmp_path)
    assert C._mark_failed(str(tmp_path), have_lock=True) is False
    assert not (tmp_path / ".codegraph").exists()


def test_another_process_already_building_is_not_duplicated(build, tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(C, "_acquire_build_lock", lambda r: None)
    assert C.ensure_indexed(str(tmp_path)) is False
    assert build["calls"] == []


def test_an_index_that_appeared_while_we_waited_is_trusted(build, tmp_path,
                                                           monkeypatch):
    def _lock(repo):
        _index(tmp_path, db="ok")         # the other process finished
        return "nolock"
    monkeypatch.setattr(C, "_acquire_build_lock", _lock)
    assert C.ensure_indexed(str(tmp_path)) is True
    assert build["calls"] == []


def test_autobuild_can_be_turned_off(build, tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CODEGRAPH_AUTOBUILD", "0")
    assert C.ensure_indexed(str(tmp_path)) is False
    assert build["calls"] == []


def test_nothing_is_built_when_codegraph_is_disabled(build, tmp_path,
                                                     monkeypatch):
    monkeypatch.setenv("AIFORGE_CODEGRAPH_DISABLE", "1")
    assert C.ensure_indexed(str(tmp_path)) is False
    assert build["calls"] == []


def test_a_repo_folder_that_is_not_there_is_not_built(build, monkeypatch):
    monkeypatch.setenv("AIFORGE_CODEGRAPH_PATH", "/nope/repo")
    assert C.ensure_indexed("/nope/repo") is False
    assert build["calls"] == []


def test_the_build_command_and_timeout_are_overridable(monkeypatch):
    monkeypatch.setenv("AIFORGE_CODEGRAPH_INIT_CMD", "init --force")
    monkeypatch.setenv("AIFORGE_CODEGRAPH_BUILD_TIMEOUT_S", "20")
    assert C._init_cmd() == "init --force"
    assert C._build_timeout_s() == 20
    monkeypatch.setenv("AIFORGE_CODEGRAPH_BUILD_TIMEOUT_S", "5")
    assert C._build_timeout_s() == 10, "with a floor"
    monkeypatch.setenv("AIFORGE_CODEGRAPH_BUILD_TIMEOUT_S", "soon")
    assert C._build_timeout_s() == 180


def test_the_init_path_is_positional(monkeypatch, tmp_path):
    """`--path` there made the binary reject it, so autobuild silently failed."""
    seen: dict = {}
    monkeypatch.setattr(C.subprocess, "run",
                        lambda argv, **kw: seen.update(argv=argv, kw=kw)
                        or pytypes.SimpleNamespace(returncode=0))
    monkeypatch.setenv("AIFORGE_CODEGRAPH_INIT_CMD", "init --force")
    C._run_init("/usr/bin/codegraph", str(tmp_path), 30)
    assert seen["argv"] == ["/usr/bin/codegraph", "init", "--force",
                            str(tmp_path)]
    assert seen["kw"]["timeout"] == 30


# ─── querying ──────────────────────────────────────────────────────────


@pytest.fixture
def query(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CODEGRAPH_PATH", str(tmp_path))
    state: dict = {"rc": 0, "out": "results", "err": "", "raise": None,
                   "argv": None}
    monkeypatch.setattr(C, "_bin", lambda: "/usr/bin/codegraph")

    def _run(argv, **kw):
        state["argv"] = list(argv)
        if state["raise"]:
            raise state["raise"]
        return pytypes.SimpleNamespace(returncode=state["rc"],
                                       stdout=state["out"], stderr=state["err"])
    monkeypatch.setattr(C.subprocess, "run", _run)
    return state


@pytest.mark.parametrize("fn,key,sub", [
    (C.codegraph_query, "query", "query"),
    (C.codegraph_callers, "symbol", "callers"),
    (C.codegraph_callees, "symbol", "callees"),
    (C.codegraph_impact, "symbol", "impact"),
    (C.codegraph_explore, "query", "explore"),
])
def test_each_query_runs_its_subcommand_against_the_repo(query, fn, key, sub,
                                                          tmp_path):
    out = fn({key: "publishToRemoteServer"})
    assert out == {"ok": True, "result": "results"}
    assert query["argv"][1] == sub
    assert query["argv"][-2:] == ["--path", str(tmp_path)]


@pytest.mark.parametrize("fn,err", [
    (C.codegraph_query, "missing 'query'"),
    (C.codegraph_callers, "missing 'symbol'"),
    (C.codegraph_callees, "missing 'symbol'"),
    (C.codegraph_impact, "missing 'symbol'"),
    (C.codegraph_explore, "missing 'query'"),
])
def test_each_query_needs_its_argument(query, fn, err):
    assert fn({})["error"] == err
    assert fn({"query": "  ", "symbol": " "})["error"] == err


def test_the_search_alias_is_accepted(query):
    C.codegraph_query({"search": "sync"})
    assert query["argv"][2] == "sync"


def test_a_failed_query_surfaces_stderr(query):
    query["rc"] = 1
    query["out"] = ""
    query["err"] = "no index for this repo"
    assert C.codegraph_query({"query": "x"}) == {
        "ok": False, "error": "no index for this repo"}


def test_a_degraded_result_is_returned_with_its_warning(query):
    """Partial output must not be passed off as authoritative."""
    query["rc"] = 1
    query["err"] = "3 files failed to parse"
    out = C.codegraph_query({"query": "x"})
    assert out["ok"] is True
    assert out["warning"] == "3 files failed to parse"


def test_a_huge_result_is_capped(query):
    query["out"] = "z" * (C._CAP + 100)
    assert len(C.codegraph_query({"query": "x"})["result"]) == C._CAP


def test_a_query_that_hangs_is_reported(query):
    query["raise"] = C.subprocess.TimeoutExpired("codegraph", 30)
    assert C.codegraph_query({"query": "x"})["error"] == "codegraph timed out"


def test_a_spawn_failure_is_reported(query):
    query["raise"] = OSError("exec format error")
    assert "exec format" in C.codegraph_query({"query": "x"})["error"]


def test_without_the_binary_the_install_command_is_offered(query, monkeypatch):
    monkeypatch.setattr(C, "_bin", lambda: None)
    assert "npm i -g" in C.codegraph_query({"query": "x"})["error"]
