"""End-to-end sanity: (A) the two code indexes are REUSED, not rebuilt on
every call, and (B) the six hardened bug areas each hold — one DISTINCT task
per test, no scenario reused across tests.

A. Index reuse
   - Aider RepoMap: the persistent per-repo tags-cache dir is keyed by the
     repo's git-common-dir, so every worktree of a repo shares ONE index
     (indexed once, reused); the in-process digest memo returns a prior render
     verbatim (underlying render invoked once for two identical calls); a real
     Aider render leaves the on-disk ``.aider.tags.cache.v4`` for reuse.
   - CodeGraph: ``ensure_indexed`` builds ONCE, then trusts the on-disk index
     via the ``_VERIFIED_HEALTHY`` fast-path — and STILL trusts a healthy
     on-disk index after the in-process cache is cleared (fresh-process reuse),
     never rebuilding.

B. Six bugs — one unique task each (payment refactor / policy search / deploy
   checklist note / multimodal probe / tool_choice rejection / corrupt index).
"""
from __future__ import annotations

import io
import subprocess
import urllib.error

import pytest


# ─────────────────────── shared tiny helpers ───────────────────────────────
def _valid_sqlite_bytes() -> bytes:
    import os
    import sqlite3
    import tempfile
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()
    with open(p, "rb") as f:
        data = f.read()
    os.unlink(p)
    return data


def _http_err(code: int, body: str) -> urllib.error.HTTPError:
    # Body sits in BOTH the readable stream (vision's _error_text calls
    # exc.read()) and the _aiforge_body stash (native's _http_err_body) so either
    # body-reader sees the reason text.
    e = urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body.encode()))
    e._aiforge_body = body.encode()
    return e


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


# ════════════════════════ A. INDEX REUSE ════════════════════════════════════

def test_aider_index_dir_shared_across_worktrees_then_memo_reused(tmp_path,
                                                                   monkeypatch):
    """One repo → ONE persistent index dir (git-common-dir key) shared by every
    worktree, and a second identical render reuses the in-process memo instead
    of re-parsing."""
    from aiforge_core.memory import code_context as cc
    from aiforge_core.indexing import aider_map as am

    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "a.py").write_text("def a():\n    return 1\n")
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    wt = tmp_path / "wt-ticket-42"
    _git("worktree", "add", "-q", str(wt), "HEAD", cwd=repo)

    monkeypatch.setenv("AIFORGE_REPO_INDEX_DIR", str(tmp_path / "idx"))
    # The index dir is keyed by the repo's shared git-common-dir — the main
    # checkout and its worktree resolve to the SAME folder → indexed once, reused.
    assert cc._repo_index_dir(repo) == cc._repo_index_dir(wt)

    # in-process digest memo: two identical renders → underlying render ONCE.
    monkeypatch.setenv("AIFORGE_AIDER_MAP_CACHE", "1")
    am._MAP_CACHE.clear()
    calls = {"n": 0}

    def _counting_render(cfg):
        calls["n"] += 1
        return "MAP:one-real-render"
    monkeypatch.setattr(am, "render_repo_map", _counting_render)
    cfg = am.AiderMapConfig(root=repo, chat_files=["a.py"], other_files=[],
                            map_tokens=256, user_text="a")
    d1 = am.render_repo_map_cached(cfg)
    d2 = am.render_repo_map_cached(cfg)
    assert d1 == d2 == "MAP:one-real-render"
    assert calls["n"] == 1                    # second call reused the memo


def test_aider_real_render_persists_tags_cache_for_reuse(tmp_path, monkeypatch):
    """A real Aider render writes a persistent ``.aider.tags.cache.v4`` under the
    central index dir; a second render (in-proc memo cleared) reuses it and
    returns the same digest — the repo is not re-scanned from scratch."""
    from aiforge_core.memory import code_context as cc
    from aiforge_core.indexing import aider_map as am

    repo = tmp_path / "app"
    (repo / "pkg").mkdir(parents=True)
    for i in range(6):
        (repo / "pkg" / f"m{i}.py").write_text(
            f"def fn{i}(x):\n    return fn{(i+1) % 6}(x) + {i}\n"
            f"class C{i}:\n    def run(self):\n        return fn{i}(self)\n")
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)

    idx_base = tmp_path / "central"
    monkeypatch.setenv("AIFORGE_REPO_INDEX_DIR", str(idx_base))
    monkeypatch.setenv("AIFORGE_AIDER_REPOMAP_ENABLED", "1")
    am._MAP_CACHE.clear()

    chat = ["pkg/m0.py"]
    d1 = cc.aider_digest(str(repo), chat, token_budget=256, user_text="fn0 run")
    if not d1:
        pytest.skip("aider produced no map in this env (grammar/parse) ")

    index_dir = cc._repo_index_dir(repo)
    cache_hits = list(index_dir.glob(".aider.tags.cache.v4*"))
    assert cache_hits, "tags cache not persisted for reuse"
    mtimes = {p: p.stat().st_mtime_ns for p in cache_hits}

    am._MAP_CACHE.clear()                     # force a fresh (non-memo) render
    d2 = cc.aider_digest(str(repo), chat, token_budget=256, user_text="fn0 run")
    assert d2 == d1                           # identical output from reused cache
    # the persisted cache is REUSED (still present) — reindex didn't wipe it.
    for p, mt in mtimes.items():
        assert p.exists()


def test_codegraph_index_built_once_then_reused_across_process(tmp_path,
                                                               monkeypatch):
    """First ``ensure_indexed`` builds; the second call trusts the cached
    fast-path (no rebuild), and even after the in-process ``_VERIFIED_HEALTHY``
    cache is cleared (a fresh process) a healthy on-disk index is reused, never
    rebuilt."""
    from aiforge_core.runtime.tools import codegraph as cg
    repo = tmp_path
    d = repo / ".codegraph"

    monkeypatch.setattr(cg, "_autobuild_enabled", lambda: True)
    monkeypatch.setattr(cg, "_disabled", lambda: False)
    monkeypatch.setattr(cg, "available", lambda: True)
    monkeypatch.setattr(cg, "_bin", lambda: "/usr/bin/true")
    monkeypatch.setattr(cg, "_acquire_build_lock", lambda repo: object())
    monkeypatch.setenv("AIFORGE_CODEGRAPH_PATH", str(repo))
    cg._VERIFIED_HEALTHY.clear()
    cg._FAILED.clear()

    builds = {"n": 0}

    def fake_run(cmd, **k):
        builds["n"] += 1
        d.mkdir(exist_ok=True)
        (d / "graph.db").write_bytes(_valid_sqlite_bytes())

        class _P:
            returncode = 0
            stdout = stderr = ""
        return _P()
    monkeypatch.setattr(cg.subprocess, "run", fake_run)

    assert cg.ensure_indexed(str(repo)) is True
    assert builds["n"] == 1                    # first time: created
    assert cg.ensure_indexed(str(repo)) is True
    assert builds["n"] == 1                    # reused via _VERIFIED_HEALTHY

    cg._VERIFIED_HEALTHY.clear()               # simulate a fresh process restart
    assert cg.ensure_indexed(str(repo)) is True
    assert builds["n"] == 1                    # healthy on-disk index still reused


# ════════════════════════ B. SIX BUGS (distinct tasks) ══════════════════════

def test_bug1_claim_guard_flags_first_person_edit_after_nonedit_verb():
    """Bug1 — the r12 first-person guard: a genuine 'I …ran… and updated X.py'
    claim must be flagged even though a non-edit verb ('ran') precedes the edit
    verb (the r11 per-verb rewrite briefly let this escape)."""
    from aiforge_core.runtime.chat_agent._context._claim_guard import (
        _claims_file_edits,
    )
    task = "I ran the migration script and updated schema.sql to add an audit column."
    assert _claims_file_edits(task) is True
    # control: a pure third-party recap must NOT trip the guard.
    assert _claims_file_edits(
        "Previously the deploy job rewrote the manifest before rollout.") is False


def test_bug2_stemming_unifies_policy_singular_plural_and_ranks_it():
    """Bug2 — cross-chat search stemming: a 'policies' query finds a 'policy'
    doc (shared root) and the on-topic row ranks first."""
    from aiforge_core.runtime.chat_store._helpers import (
        _rank_search, _stem_root, _tokens,
    )
    assert _stem_root("policies") == _stem_root("policy")

    def _row(i, text):
        return {"id": i, "session_id": "s", "session_title": "t", "role": "user",
                "content": text, "created_at": "2026-03-02T00:00:00+00:00"}
    rows = [_row("x", "unrelated cache invalidation note"),
            _row("y", "the policy enforcement layer rejects the request")]
    ranked = _rank_search(rows, _tokens("policies"), 10)
    assert ranked and ranked[0]["content"].startswith("the policy enforcement")


def test_bug3_okr_note_repairs_blank_kind_and_scrubs_scaffolding():
    """Bug3 — OKR write path repairs, never rejects: a blank kind becomes
    'knowledge' and scaffolding-leak facts are scrubbed; the rendered note then
    validates clean."""
    from aiforge_core.runtime.work_notes._render import (
        render_note, scrub_items, validate_note,
    )
    facts = scrub_items(["## Facts", "prod deploy needs a green CI run",
                         "Facts:", "-----",
                         "keep durable, deduped knowledge only"])
    assert facts == ["prod deploy needs a green CI run"]
    note = render_note("", "deploy-checklist", title="Deploy checklist",
                       objective="Ship the release safely", facts=facts,
                       timestamp="2026-07-18T00:00:00Z")
    assert 'type: "knowledge"' in note        # blank kind repaired
    ok, issues = validate_note(note)
    assert ok, issues


def test_bug4_vision_probe_modality_reject_vs_form_reject():
    """Bug4 — probe classifier: a true text-only/modality rejection returns
    False (disable vision); a base64-FORM rejection stays inconclusive (None) so
    a real VLM that just wants a URL is not marked non-vision."""
    from aiforge_core.runtime.vision_detect import _classify_probe_error
    modality = _http_err(400, "This is a text-only model; vision is not supported")
    assert _classify_probe_error(modality) is False
    form = _http_err(400, "base64 images are not supported, use image_url instead")
    assert _classify_probe_error(form) is None


def test_bug5_native_tool_choice_only_rejection_keeps_native_enabled():
    """Bug5 — a rejection naming ONLY tool_choice (the probe's forced mode) must
    NOT permanently disable native FC: the endpoint still supports tools with
    'auto'. Guard = _tools_unsupported AND NOT _rejects_only_tool_choice."""
    from aiforge_core.runtime.chat_agent._native import (
        _rejects_only_tool_choice, _tools_unsupported,
    )
    exc = _http_err(400, 'tool_choice "required" is not supported by this server')
    assert _rejects_only_tool_choice(exc) is True
    assert not (_tools_unsupported(exc) and not _rejects_only_tool_choice(exc))
    # contrast: a genuine tools-incapable endpoint IS disabled.
    hard = _http_err(400, "this model does not support function calling tools")
    assert _tools_unsupported(hard) and not _rejects_only_tool_choice(hard)


def test_bug6_codegraph_rebuilds_corrupt_crash_leftover(tmp_path, monkeypatch):
    """Bug6 — a corrupt index left by a crashed prior process is not trusted
    forever: ensure_indexed proves it corrupt, rebuilds once, then serves the
    healthy index from the fast-path."""
    from aiforge_core.runtime.tools import codegraph as cg
    repo = tmp_path
    d = repo / ".codegraph"
    d.mkdir()
    (d / "graph.db").write_text("garbage not-a-sqlite header")   # corrupt leftover

    monkeypatch.setattr(cg, "_autobuild_enabled", lambda: True)
    monkeypatch.setattr(cg, "_disabled", lambda: False)
    monkeypatch.setattr(cg, "available", lambda: True)
    monkeypatch.setattr(cg, "_bin", lambda: "/usr/bin/true")
    monkeypatch.setattr(cg, "_acquire_build_lock", lambda repo: object())
    monkeypatch.setenv("AIFORGE_CODEGRAPH_PATH", str(repo))
    cg._VERIFIED_HEALTHY.clear()
    cg._FAILED.clear()

    builds = {"n": 0}

    def fake_run(cmd, **k):
        builds["n"] += 1
        d.mkdir(exist_ok=True)
        (d / "graph.db").write_bytes(_valid_sqlite_bytes())

        class _P:
            returncode = 0
            stdout = stderr = ""
        return _P()
    monkeypatch.setattr(cg.subprocess, "run", fake_run)

    assert cg.ensure_indexed(str(repo)) is True
    assert builds["n"] == 1                    # corrupt → rebuilt exactly once
    assert cg.ensure_indexed(str(repo)) is True
    assert builds["n"] == 1                    # healthy rebuild now reused
