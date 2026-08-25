"""One sync cycle, and the scheduler that repeats it.

Hub and spoke. A spoke pushes what it authored to the admin and pulls what the
admin distilled; the admin talks to nobody — it only answers. There is no
registry to read, no discovery to run and no election to compute: which machine
is the admin is configuration (``role``), not something derived from replicated
state.

The admin's cycle is therefore just the fold. It still runs on this loop rather
than a schedule of its own — one moving part — and skips when its inputs are
unchanged, so an idle hub costs a directory walk and no tokens.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger("aiforge.sync")

DEFAULT_INTERVAL = 1800  # 30 minutes

# How long one cycle may spend talking to the admin before it stops starting new
# requests. Per-request deadlines bound a single request
# (``transport.REQUEST_DEADLINE``); they do not bound their sum, and one cycle
# may push or fetch up to MAX_MANIFEST_ENTRIES blobs. A third of the interval
# leaves two thirds for compaction and the sleep; whatever is left over is
# offered again next cycle, which is what an unreachable admin costs anyway.
CYCLE_BUDGET = DEFAULT_INTERVAL // 3

# How many consecutive failed cycles before the log says so. A cycle that fails
# once is weather; a cycle that fails every time is a broken machine — a full
# disk, a state file nobody can parse — and the two used to produce the same
# line forever, so the second was invisible until somebody noticed sync had been
# dead for a week.
REPEATED_FAILURES = 5


def _ingest(entries) -> list[dict]:
    """Normalise the admin's manifest into the form the merge compares in.

    Only the hash needs it: the local side is a ``hexdigest`` and is therefore
    lowercase, so an admin emitting uppercase hex matches nothing we hold. Every
    round it produces the same unresolvable conflict, and the entry can never be
    applied — the two spellings never become equal on their own.
    """
    out: list[dict] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue          # the admin may send anything; a non-record is not one
        row = dict(e)
        row["hash"] = str(row.get("hash") or "").strip().lower()
        out.append(row)
    return out


def _spent(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def sync_with(base_url: str, deadline: float | None = None) -> dict:
    """Run one cycle against the admin: push first, then pull.

    ``deadline`` is a ``time.monotonic`` stamp after which the cycle stops
    starting requests — see ``run_once``. Optional so that a caller syncing on
    purpose (the admin page's "sync now") is not forced to invent a budget.

    Push first so the fold that runs on the admin at the end of *its* cycle sees
    what we just authored, rather than always folding a cycle behind.

    Returns ``{ok, pushed, applied, rejected, conflicts}``. Never raises, and the
    counts survive a failure part-way through: under a global ENOSPC the
    per-entry applies were correctly counted as rejected and then the
    bookkeeping raised, so the whole row was discarded and one WARNING was the
    only trace the cycle had run at all.
    """
    from aiforge_core.memory.sync import push

    result = {"ok": False, "pushed": 0, "applied": 0, "rejected": 0, "conflicts": 0}
    try:
        up = push.run_once(base_url, deadline)
        result["pushed"] = up["pushed"]
        result["rejected"] += up["rejected"]
        result["ok"] = up["ok"]
        _pull(base_url, result, deadline)
    except Exception as exc:  # noqa: BLE001 — a misbehaving admin is not our death
        _log.warning("sync: admin %s failed mid-cycle: %s", base_url, exc)
    return result


def _preserve_conflicts(base_url: str, plan: dict, result: dict,
                        deadline, transport, apply) -> None:
    """Keep the copy the merge is about to discard.

    A conflicting remote entry that also appears in ``want`` is the winner, so
    the LOCAL copy is what is about to be lost; otherwise the local copy stays
    and the remote's text is the version nothing else preserves.
    """
    winning = {str(e.get("hash") or "") for e in plan["want"]}
    for pair in plan["conflict"]:
        if _spent(deadline):
            return
        losing_body = None
        if str(pair["remote"].get("hash") or "") not in winning:
            losing_body = transport.fetch_blob(
                base_url, str(pair["remote"].get("hash") or ""))
            if losing_body is None:
                continue      # nothing fetched, nothing to preserve
        if apply.keep_conflict(pair["local"], losing_body):
            result["conflicts"] += 1


def _apply_one(entry: dict, body, admin: str, apply) -> bool:
    """``admin`` is load-bearing, not bookkeeping: apply refuses any class B
    entry whose ``origin`` is not the machine that served it, so dropping it
    would let the admin forge a spoke's nodes and tombstones. It also means an
    admin whose id we have not learned yet applies nothing this cycle rather
    than everything."""
    try:
        return apply.apply_blob(entry, body, peer_id=admin)
    except OSError as exc:
        # One unwritable record — a 400-character key is "File name too long" —
        # used to abort the cycle: every later entry was discarded, and it is
        # re-advertised forever, so this must be per-entry.
        _log.warning("sync: could not apply %s: %s", entry.get("path"), exc)
        return False


def _fetch_wanted(base_url: str, plan: dict, result: dict, admin: str,
                  deadline, transport, apply) -> None:
    """Fetch + apply each wanted blob.

    ``got`` is counted locally, not off ``result``: ``result["rejected"]``
    already carries the PUSH phase's rejections, so using it here under-reported
    (and could negate) how many entries the pull still owes the next cycle.
    """
    got = 0
    for entry in plan["want"]:
        if _spent(deadline):
            # The budget has to be re-checked *inside* this loop, not only
            # around it: one manifest may advertise MAX_MANIFEST_ENTRIES
            # (20 000) blobs, so a cycle that passed the pre-flight check by a
            # millisecond could otherwise spend hours here while the compaction
            # pass behind it waits its turn. The remaining entries are still
            # advertised next cycle.
            _log.warning("sync: cycle budget spent part-way through the pull — "
                         "%d entries left for the next cycle",
                         len(plan["want"]) - got)
            return
        got += 1
        body = transport.fetch_blob(base_url, str(entry.get("hash") or ""))
        if body is None:
            result["rejected"] += 1
            continue
        applied = _apply_one(entry, body, admin, apply)
        result["applied" if applied else "rejected"] += 1


def _pull(base_url: str, result: dict, deadline: float | None = None) -> None:
    """The admin's manifest, blobs and bookkeeping, accumulated into ``result``."""
    from aiforge_core.memory.sync import apply, manifest, merge, role, transport

    remote = transport.fetch_manifest(base_url)
    if not remote:
        return
    result["ok"] = True
    # The admin states its own id in every manifest response, so a spoke learns
    # whose fold to trust (``okf.tiers._trusted_origin``) without the operator
    # configuring the same fact twice.
    admin = role.remember_admin_id(str(remote.get("admin") or ""))
    plan = merge.plan_sync(manifest.build(),
                           _ingest(remote.get("manifest") or []))
    _preserve_conflicts(base_url, plan, result, deadline, transport, apply)
    _fetch_wanted(base_url, plan, result, admin, deadline, transport, apply)
    _log.info("sync: admin=%s pushed=%d applied=%d rejected=%d conflicts=%d",
              admin or "?", result["pushed"], result["applied"],
              result["rejected"], result["conflicts"])


def run_once() -> list[dict]:
    """One cycle. Never raises.

    Returns one row for the admin we synced with, or no rows at all on the admin
    itself — it answers requests, it does not make them. The list shape is kept
    (rather than a bare dict) because the CLI, the tests and the admin page all
    iterate it, and "nothing to do" is naturally an empty list.

    The role is checked BEFORE the url, not just the url: a box started with
    ``run.sh --admin`` that also has a stale ``AIFORGE_ADMIN_URL`` in its .env
    would otherwise push its own knowledge to somebody else's admin while
    serving as an admin itself — knowledge crossing in both directions, and two
    machines both stamping ``derived: mesh``.
    """
    from aiforge_core.memory.sync import role

    if role.is_admin():
        return []
    base = role.admin_url()
    if not base:
        # A spoke with nowhere to sync: explicitly configured (AIFORGE_ROLE=spoke)
        # with no admin named. Nothing to do, and nothing to warn about every
        # cycle — the admin page shows the missing url.
        return []
    deadline = time.monotonic() + CYCLE_BUDGET
    row = {"admin": base}
    try:
        row.update(sync_with(base, deadline))
    except Exception as exc:  # noqa: BLE001 — a bad cycle is data, not a crash
        _log.warning("sync: cycle failed for %s: %s", base, exc)
    return [row]


def run_forever(interval: int = DEFAULT_INTERVAL) -> None:
    """Sync every ``interval`` seconds for as long as this process lives.

    Nothing here decides who folds: ``role`` reads it from configuration, so
    there is no record to claim and no heartbeat to keep alive.

    Knowledge compaction rides this cycle rather than owning a schedule of its
    own — one moving part. It runs *after* the sync pass so the admin folds the
    data that cycle just received, and it can never take the daemon down: both
    tiers skip when their inputs are unchanged and soft-fail when they are not.

    Only a ``BaseException`` — a signal, a ``SystemExit`` — ends this loop. Any
    ordinary ``Exception`` from a cycle is logged and the next cycle runs.
    """
    from aiforge_core.memory.okf import tiers

    # Rejected here rather than later, because there is no recovering from it:
    # ``interval=0`` turns the blanket except below into an unthrottled
    # traceback firehose, and a negative one raises out of ``time.sleep`` — the
    # single line the try cannot cover — killing a daemon whose whole design is
    # to outlive its own failures.
    if interval <= 0:
        raise ValueError(f"sync interval must be positive, got {interval}")

    failures = 0
    while True:
        try:
            run_once()
            tiers.run_after_sync()
        except Exception as exc:  # noqa: BLE001 — outliving a bad cycle is the point
            # The daemon surviving one bad cycle is the entire reason this loop
            # exists: whatever broke (disk full, a state file somebody edited,
            # an admin nobody anticipated) is almost always transient or fixable
            # while we keep running, whereas exiting means the supervisor
            # restarts us straight back into it and compaction never runs again.
            failures += 1
            if failures >= REPEATED_FAILURES:
                # A permanent on-disk fault otherwise looks exactly like a
                # transient one, forever, and nothing in the log distinguishes
                # "syncing fine" from "has not synced since Tuesday".
                _log.error("sync: %d consecutive cycles have failed — this is "
                           "not transient, sync and compaction are both "
                           "stopped: %s", failures, exc, exc_info=True)
            else:
                _log.error("sync: cycle failed, continuing: %s", exc, exc_info=True)
        else:
            failures = 0
        time.sleep(interval)


def main() -> None:
    import argparse

    from aiforge_core.memory.sync import role

    ap = argparse.ArgumentParser(description="AIForge memory sync (hub and spoke)")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    args = ap.parse_args()
    if args.interval <= 0:
        # argparse's own exit, so the operator gets usage on stderr and a
        # non-zero status instead of a daemon that spins or dies on its first
        # sleep. Checked here as well as in run_forever: this is the boundary
        # the value actually arrives at.
        ap.error("--interval must be a positive number of seconds")
    logging.basicConfig(level=logging.INFO)
    # Reads the ROLE first, like run_once does. Logging the url unconditionally
    # made the one diagnostic line an operator sees say "role=admin
    # admin=http://someone-else" — the exact configuration this line is read to
    # debug.
    if role.is_admin():
        _log.info("sync: role=admin — this machine merges; it makes no outbound call")
    elif role.admin_url():
        _log.info("sync: role=spoke admin=%s", role.admin_url())
    else:
        _log.warning("sync: role=spoke but no AIFORGE_ADMIN_URL is set — this "
                     "machine will neither sync nor merge")
    if args.once:
        for row in run_once():
            print(row)
        return
    run_forever(args.interval)


__all__ = ["sync_with", "run_once", "run_forever", "main"]


if __name__ == "__main__":  # pragma: no cover — exercised by test_cli_entry
    # Without this, `python -m aiforge_core.memory.sync.loop` imports the module
    # and exits silently: the console script reaches main() but -m does not.
    main()
