"""The scheduled memory compaction: hourly and daily passes, the idle-session
pass, and the artifact merge, registered with the jobs scheduler."""
from __future__ import annotations

import os

from aiforge_core.api.routes import memory as _r_memory


def _pkg():
    """The package, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.api.api as package
    return package


def _compact_at_hour() -> "int | None":
    """Local hour for the single daily memory-compaction pass, or None to keep
    the old hourly/idle/nightly schedule.

    Default 18 (evening): every fold costs learner-LLM calls, so re-folding the
    same briefs all day buys little over one pass once the day's work is in.
    ``AIFORGE_COMPACT_AT_HOUR=off`` (or an explicit ``AIFORGE_COMPACT_EVERY_H``,
    which only means anything on the hourly schedule) restores the old cadence.

    Parsing lives in ``runtime.compact_window`` so the opportunistic chat folds
    read the SAME window as this scheduled pass.
    """
    from aiforge_core.runtime import compact_window
    return compact_window.at_hour()


def _compact_mode_skips(idle_only: bool) -> bool:
    """True when this pass should not fold anything at all.

    The DAILY pass folds for every mode except 'off': it IS the trigger, and
    'turns'/'explicit' with no idle daemon left would mean nothing folds.
    """
    mode = os.environ.get("AIFORGE_SESSION_COMPACT", "idle")
    if mode in ("off", "0", "false", "no"):
        return True                      # off by config, not a failure
    return idle_only and mode != "idle"   # the idle daemon only runs for 'idle'


def _compact_due(prev: dict | None, count: int, idle_only: bool) -> bool:
    """Has this session earned a fold on this pass?

    The idle daemon wants the two-scan handshake — an unchanged turn count since
    the last scan means the chat went quiet. The daily pass cannot use that (it
    would defer every session by a full day), so it takes anything with new
    turns or an unfinished walk.
    """
    if count <= 0:
        return False
    if idle_only:
        return (prev is not None and prev.get("count") == count
                and not prev.get("done"))
    return (prev or {}).get("count") != count or not (prev or {}).get("done")


def _walk_compact(sid, repo, windows: int) -> tuple[bool, bool]:
    """Fold a session's backlog; returns ``(drained, failed)``.

    WALKs the whole backlog on the daily pass. One fold only distils the turns
    that fit in AIFORGE_SESSION_COMPACT_CHARS and advances the offset by exactly
    those, so a day's chat needs several windows — folding once would leave the
    rest to a 30-min-idle daemon that no longer runs. Bounded by ``windows`` so
    a runaway session cannot hold the pass forever.
    """
    from aiforge_core.runtime import chat_okr
    for _ in range(windows):
        r = chat_okr.compact_session(sid, repo=repo) or {}
        _pkg()._af_log.info("session compact sid=%s: %s", sid, r)
        skipped = r.get("skipped")
        if skipped in ("no_new", "too_short", "disabled"):
            return True, False          # nothing left to fold for this session
        if skipped:
            # extract_failed / capture_failed / reset: the turns are still
            # pending. This is what a provider outage looks like — the pass must
            # NOT report success, or the whole retry budget never engages.
            return False, skipped in ("extract_failed", "capture_failed")
        if not r.get("ok"):
            return False, True
        if not r.get("remaining"):
            return True, False          # backlog fully folded
    return False, False                 # window cap — revisit next pass


def _session_turns(sid) -> int | None:
    from aiforge_core.runtime import chat_store
    try:
        return len(chat_store.get_messages(sid) or [])
    except Exception:  # noqa: BLE001
        return None


def _prior_scan(state: dict, sid, stamp: str) -> dict | None:
    prev = state.get(sid)
    if prev is not None and prev.get("stamp") != stamp:
        return None          # id reused after a reset — not the same chat
    return prev


def _scan_one_session(s: dict, *, idle_only: bool, state: dict,
                      max_windows: int) -> bool:
    """Fold one session if it is due. Returns True when the fold FAILED."""
    from aiforge_core.runtime.chat_agent import _chat_repo_key
    sid = (s or {}).get("id")
    count = _session_turns(sid)
    if count is None:
        return False
    stamp = str((s or {}).get("created_at") or (s or {}).get("started_at") or "")
    prev = _prior_scan(state, sid, stamp)
    if not _compact_due(prev, count, idle_only):
        if prev is None or prev.get("count") != count:
            state[sid] = {"count": count, "stamp": stamp, "done": False}
        return False
    cwd = (s or {}).get("cwd")
    repo = _chat_repo_key(cwd) if cwd else None
    try:
        drained, failed = _walk_compact(sid, repo,
                                        1 if idle_only else max_windows)
    except Exception as exc:  # noqa: BLE001
        _pkg()._af_log.warning("session compact sid=%s failed: %s", sid, exc)
        drained, failed = False, True
    # done=False when the walk STOPPED SHORT (window cap, model down, error):
    # tomorrow's pass must revisit the session even if no new message arrived,
    # or the tail of a long day is folded by nobody, ever.
    state[sid] = {"count": count, "stamp": stamp,
                  "done": drained or idle_only}
    return failed


def _axis_done(groups_done, axis: str):
    """The groups this axis already folded in the current cycle (or None)."""
    return groups_done(axis) if groups_done else None


def _axis_recorder(group_done, axis: str):
    """Record a folded group against its axis (or don't record at all)."""
    if group_done is None:
        return None
    return lambda key: group_done(axis, key)


def _compact_chat_md(should_stop=None, skip_keys=None,
                     on_group_done=None) -> "bool | str":
    """HOURLY CHAT-MD COMPACTION — per-turn writes append forever to
    ~/.aiforge/memory/*.md; md_store.compact() consolidates them (map-reduce
    summary, archives originals) so the memory folder stays bounded + legible.

    Two axes, both kept (overlap intended): per-REPO → the project brief you
    load when opening a repo; per-TOPIC → cross-repo theme notes.
    """
    try:
        from aiforge_core.memory import md_store
        # Order matters: REPO first as a non-destructive projection
        # (archive_sources=False) so every unit is folded into its project
        # brief while the raw file still exists. TOPIC runs second and
        # ARCHIVES the folded raw units (archive_sources=True) — so memory is
        # organized BY TOPIC and the per-session raw notes stop piling up in
        # the live folder (moved to archive/<ts>/, reversible). Both briefs
        # re-feed their own consolidated OKR sections on the next run, so a
        # unit's knowledge survives in both briefs after its raw file clears.
        # min_group=1: fold even a LONE note into its brief — a single
        # session is often its own topic, so min_group=2 would leave it
        # sitting raw forever ("nothing to compact"). Singletons still get
        # organized by topic + archived.
        # Each axis keeps its OWN done-set: the same key names a repo brief on
        # one axis and a topic brief on the other, so one shared set would skip
        # a group that never ran.
        r_repo = md_store.compact(group_by="repo", min_group=1, summarize=True,
                                  model_role="learner", archive_sources=False,
                                  should_stop=should_stop,
                                  skip_keys=_axis_done(skip_keys, "repo"),
                                  on_group_done=_axis_recorder(on_group_done, "repo"))
        if r_repo.get("stopped"):
            return "stopped"
        r_topic = md_store.compact(group_by="topic", min_group=1, summarize=True,
                                   model_role="learner", archive_sources=True,
                                   should_stop=should_stop,
                                   skip_keys=_axis_done(skip_keys, "topic"),
                                   on_group_done=_axis_recorder(on_group_done, "topic"))
        if r_topic.get("stopped"):
            return "stopped"
        # Retire per-run captures that masquerade as canonical briefs
        # (compacted-<desc>-YYYYMMDD-hex.md) — compact() can never see them,
        # so they'd pile up forever; their facts already live in the real
        # compacted-<topic>.md brief. Archive them out (reversible).
        r_sweep = md_store.sweep_stale_captures(archive=True)
        # Retire DEAD briefs — a compacted-<key>.md left with only the
        # boilerplate Objective (facts migrated elsewhere / emptied /
        # compacted-compacted-* artifact). They read as "empty" memories.
        r_empty = md_store.sweep_empty_briefs(archive=True)
        # …and retire what has sat in archive/ past the retention window: every
        # sweep above MOVES files in there and nothing ever took them out.
        md_store.prune_archive()
        # Apply the CROSS-BRIEF rules on every compaction (not just Compact
        # all): merge topics, drop global-dup facts, resolve contradictions
        # (latest wins), sweep emptied stubs, lint + (re)link briefs. Without
        # this the hourly/Compact path never linked or deduped across briefs.
        r_rules = md_store.finalize_briefs(role="learner", recent_only=True)
        _pkg()._af_log.info("md brief: repo=%s topic=%s sweep=%s empty=%s rules=%s",
                     r_repo, r_topic, r_sweep, r_empty, r_rules)
        return True
    except Exception as exc:  # noqa: BLE001
        _pkg()._af_log.warning("md compaction failed: %s", exc)
        return False


def _dedupe_memory() -> None:
    """Daily SEMANTIC DEDUP of the embedded memory store — write_unit only
    dedups exact (repo,text); paraphrases pile up. Collapses near-duplicates on
    the stored vectors (no sidecar)."""
    try:
        from aiforge_core.memory import backend_select
        if backend_select.memory_backend() != "sqlite":
            return
        from aiforge_core.memory import sqlite_memory
        _pkg()._af_log.info("memory dedup: %s", sqlite_memory.dedupe())
    except Exception as exc:  # noqa: BLE001
        _pkg()._af_log.warning("memory dedup failed: %s", exc)


def _recompact_all() -> bool:
    """Daily FULL RECOMPACT — the hourly chat-compact only folds briefs with NEW
    live captures; a fact-only brief whose topic saw no new note keeps raw Facts
    in its inbox, never LLM-consolidated into prose. Once a day, force a full
    recompact so EVERY brief is re-folded through the model (dedupe / supersede
    / re-map its accumulated facts), then dedupe + repo-profiles + reingest.
    Heavy (LLM per brief) → daily, off-peak, opt-out via
    AIFORGE_RECOMPACT_DAILY=0. Serializes against manual compact-all on
    _COMPACT_LOCK, so overlap is safe."""
    try:
        from aiforge_core.memory import migrations
        _pkg()._af_log.info("daily recompact-all: %s", migrations.force_recompact_all())
        return True
    except Exception as exc:  # noqa: BLE001
        _pkg()._af_log.warning("daily recompact-all failed: %s", exc)
        return False


def _compact_idle_sessions(idle_only: bool = True) -> bool:
    """Fold every session that has earned it. False = a fold FAILED.

    idle_only=False (the daily pass): fold EVERY session that has new turns.
    compact_session is offset-based, so a session still in flight loses
    nothing — tomorrow's pass picks up the turns added after this one.
    """
    if _compact_mode_skips(idle_only):
        return True
    try:
        from aiforge_core.runtime import chat_store
        sessions = chat_store.list_sessions() or []
    except Exception as exc:  # noqa: BLE001
        _pkg()._af_log.warning("session-okr scan setup failed: %s", exc)
        return False
    max_windows = _int_env_or("AIFORGE_SESSION_COMPACT_MAX_WINDOWS", 20)
    failed = False
    for s in sessions:
        if (s or {}).get("id") is None:
            continue
        failed = _scan_one_session(
            s, idle_only=idle_only, state=_pkg()._SESSION_SCAN_STATE,
            max_windows=max_windows) or failed
    live = {(s or {}).get("id") for s in sessions}
    for sid in tuple(_pkg()._SESSION_SCAN_STATE):
        if sid not in live:
            _pkg()._SESSION_SCAN_STATE.pop(sid, None)
    return not failed


def _int_env_or(key: str, default: int, *, low: int = 1) -> int:
    try:
        return max(low, int(os.environ.get(key, str(default))))
    except (TypeError, ValueError):
        return default


def _daily_compact() -> None:
    """THE one evening pass. Order matters: sessions → captures first, then
    captures → briefs, then the full re-fold of every brief, so a day's chat
    reaches its brief in the SAME pass instead of waiting a day."""
    today = _pkg()._date.today()
    if _pkg()._PASS_DONE["day"] != today:
        _pkg()._PASS_DONE.update(day=today, stages=set())
    recompact_on = os.environ.get("AIFORGE_RECOMPACT_DAILY", "1") != "0"
    ok = True
    # Each stage is isolated: as three separately registered tasks one could
    # not cancel the others, and folding them into one function must not
    # quietly reintroduce that coupling.
    stages = (("sessions", lambda: _compact_idle_sessions(idle_only=False)),
              ("briefs", _compact_chat_md),
              ("recompact", _recompact_all if recompact_on else lambda: True))
    for stage, run in stages:
        if stage in _pkg()._PASS_DONE["stages"]:
            continue                     # already done today — skip on retry
        try:
            if run():
                _pkg()._PASS_DONE["stages"].add(stage)
            else:
                ok = False
        except Exception as exc:  # noqa: BLE001
            _pkg()._af_log.warning("daily compaction stage %s failed: %s", stage, exc)
            ok = False
    if not ok:
        # RAISE so the scheduler retries (bounded) instead of counting a pass
        # that did nothing as today's compaction.
        raise RuntimeError("daily compaction pass failed — see warnings")


def _register_hourly_jobs(_pd, hour: int) -> None:
    """The jobs that run regardless of which compaction schedule is in force."""
    # Run the INCREMENTAL reindex frequently (default every 3h), not once a day,
    # so all indexed layers (chunks + tree-sitter symbols + graphify) refresh
    # within hours of a commit. Cheap: reindex_all merkle-skips unchanged repos,
    # so an idle tick is a near-instant no-op; only a CHANGED repo pays.
    every_h = _int_env_or("AIFORGE_REINDEX_EVERY_H", 3)
    _pd.register("reindex", _r_memory._spawn_reindex_all, every_s=every_h * 3600)
    _pd.register("memory-dedup", _dedupe_memory,
                 at_hour=max(0, min(23, hour + 3)))


def _register_legacy_compaction(_pd) -> None:
    """The old hourly/idle/nightly schedule (AIFORGE_COMPACT_AT_HOUR=off)."""
    _pd.register("chat-compact", _compact_chat_md,
                 every_s=_int_env_or("AIFORGE_COMPACT_EVERY_H", 1) * 3600)
    _pd.register("session-okr-compact", _compact_idle_sessions,
                 every_s=max(300, _int_env_or("AIFORGE_SESSION_IDLE_MIN", 30) * 60))
    # A NIGHT local hour (AIFORGE_RECOMPACT_HOUR, default 02:00 local) — a
    # dedicated knob, NOT tied to the reindex hour, so the heavy nightly
    # compact-all lands off-peak regardless of when reindex runs.
    if os.environ.get("AIFORGE_RECOMPACT_DAILY", "1") != "0":
        _pd.register("recompact-all", _recompact_all,
                     at_hour=max(0, min(23, _int_env_or(
                         "AIFORGE_RECOMPACT_HOUR", 2, low=0))))


def _idle_sessions_stage(cp) -> str:
    """Fold every session with new turns, yielding between sessions."""
    if _compact_mode_skips(False):
        return "done"
    try:
        from aiforge_core.runtime import chat_store
        sessions = chat_store.list_sessions() or []
    except Exception as exc:  # noqa: BLE001
        _pkg()._af_log.warning("idle compaction: session scan failed: %s", exc)
        return "failed"
    max_windows = _int_env_or("AIFORGE_SESSION_COMPACT_MAX_WINDOWS", 20)
    failed = False
    for s in sessions:
        if (s or {}).get("id") is None:
            continue
        if cp.should_stop():
            return "stopped"
        failed = _scan_one_session(s, idle_only=False, state=_pkg()._SESSION_SCAN_STATE,
                                   max_windows=max_windows) or failed
    return "failed" if failed else "done"


def _idle_briefs_stage(cp) -> str:
    """Fold the md briefs, RESUMING at the group the last window stopped on.

    Without the checkpoint's per-group memory every interruption sent the next
    idle window back to group one — on a box used in short bursts the early
    groups were re-folded (LLM calls and all) over and over and the later ones
    were never reached."""
    res = _compact_chat_md(should_stop=cp.should_stop,
                           skip_keys=cp.groups_done, on_group_done=cp.group_done)
    if res == "stopped":
        return "stopped"
    return "done" if res else "failed"


def _idle_recompact_stage(cp) -> str:
    if os.environ.get("AIFORGE_RECOMPACT_DAILY", "1") == "0":
        return "done"
    try:
        from aiforge_core.memory import migrations
        out = migrations.force_recompact_all(checkpoint=cp)
    except Exception as exc:  # noqa: BLE001
        _pkg()._af_log.warning("idle compaction: full re-fold failed: %s", exc)
        return "failed"
    if out.get("stopped"):
        return "stopped"
    # Soft-failed steps mean the cycle did NOT do its work — retry it (bounded
    # by _MAX_STAGE_FAILURES) rather than recording the stage as complete.
    if out.get("failed_steps"):
        _pkg()._af_log.warning("idle compaction: re-fold steps failed: %s",
                        ", ".join(out["failed_steps"]))
        return "failed"
    return "done"


def _idle_compact() -> None:
    """The idle pass: compact (or resume compacting) only while nobody is using
    AIForge — see runtime.compact_idle."""
    from aiforge_core.runtime import compact_idle
    outcome = compact_idle.run_when_idle([
        ("sessions", _idle_sessions_stage),
        ("briefs", _idle_briefs_stage),
        ("recompact", _idle_recompact_stage),
    ])
    if outcome not in ("busy", "not-due"):
        _pkg()._af_log.info("idle compaction: %s", outcome)


def _register_idle_compaction(_pd) -> None:
    """COMPACTION WHENEVER IDLE (default): a cheap check every
    AIFORGE_COMPACT_CHECK_S (300 s) that compacts — resuming any unfinished
    cycle — only while nobody is using the box."""
    _pd.register("idle-compact", _idle_compact,
                 every_s=_int_env_or("AIFORGE_COMPACT_CHECK_S", 300, low=30))


def _register_daily_compaction(_pd, daily_hour: int) -> None:
    """ONE COMPACTION A DAY, IN THE EVENING (default).

    Every local fold is LLM-heavy, and running them hourly / per idle session
    spends tokens all day re-folding briefs that barely moved.

    STRICT hour: the missed-slot catch-up must NOT drag this pass into the
    working day. The whole point of the evening slot is that the LLM-heavy fold
    happens when the operator is done — a laptop that was asleep at 18:00
    yesterday would otherwise start compacting at 09:00 the next morning, which
    is exactly the intrusion the schedule exists to remove. It simply waits for
    today's 18:00 instead. AIFORGE_COMPACT_CATCH_UP=1 restores run-at-next-wake.
    """
    from aiforge_core.runtime import compact_window as _cw
    _pd.register("daily-compact", _daily_compact, at_hour=daily_hour,
                 strict_hour=not _cw.catch_up_enabled(),
                 strict_max_skip_days=_int_env_or(
                     "AIFORGE_COMPACT_MAX_SKIP_DAYS", 3, low=0))


def _register_artifact_merge(_pd) -> None:
    """Nightly library merge — fold duplicate rules / skills / workflows.

    They accumulate because the writers key on a slug: "run tests first" and
    "always run the tests" are two files saying one thing, and every one of
    them is prompt overhead on turns that never use it. Runs in the small hours
    (AIFORGE_MERGE_HOUR, default 04:00) at role=learner, so it is under the same
    rate ceiling as every other unattended sender; AIFORGE_ARTIFACT_MERGE=0
    turns it off.

    NOT strict_hour: unlike compaction there is no working-day intrusion to
    avoid — the pass is a handful of small completions — so a laptop that was
    asleep at 04:00 should still reconcile the library at the next wake.
    """
    from aiforge_core.runtime import artifact_merge as _am
    if not _am.enabled():
        return
    # Clamped to a real hour: periodic treats at_hour as [0-23] and a typo'd
    # 25 would give a task whose next-run time never arrives.
    hour = min(23, _int_env_or("AIFORGE_MERGE_HOUR", 4, low=0))
    _pd.register("artifact-merge", _am.scheduled_pass, at_hour=hour)
