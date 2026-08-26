"""What sync is doing, in one record — and how it stays quiet while it cannot.

Two jobs that belong together because they are two views of one fact:

* **The record.** One JSON file the settings screen reads. It is written every
  cycle and is the ONLY place a UI has to look to answer "is this syncing, with
  whom, into which group, and what is it waiting on". Served rather than
  probed: a page load must not be the thing that discovers the admin is down,
  because that turns a render into a twenty-second hang.
* **The quiet.** An unreachable admin is ordinary — a laptop off the LAN, a hub
  being rebooted — and before this every failed cycle logged a line. A machine
  away for a week produced hundreds of identical lines, and not one of them
  distinguished "the admin is off" from "this machine is broken". Now the state
  CHANGE is what logs.

``pending`` deserves its own note, because it is the field an operator will
mistrust. It is not a queue: it is the length of the offer's ``want`` list,
recomputed from the tree every cycle. A successful push makes that entry no
longer wanted, so pending falls to zero by construction — there is no outbox to
drain, to drift, or to clear by hand, and no state that can disagree with the
tree.

Everything here soft-fails. Losing a line of bookkeeping must never fail a cycle
that otherwise worked.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from aiforge_core.config.paths import config_dir
from aiforge_core.memory.sync import _io

_log = logging.getLogger("aiforge.sync")

_FILE = "sync_status.json"
_BLOCKS_FILE = "sync_blocks.json"

# How long a *continuing* outage stays silent before one line records that it is
# still out, and for how long. An hour makes a day offline 24 lines rather than
# 120, while still leaving an outage visible in a log covering a working day.
QUIET_SECONDS = 3600.0

# How many filter decisions to keep. Enough to see a pattern across a few days
# of cycles, small enough that the file stays a glance rather than a report.
MAX_BLOCKS = 200

# Per-admin: (last_error, when_it_was_logged). Process state, deliberately NOT
# persisted — a restart logging one line about a still-down admin is correct,
# and a persisted version would need its own staleness rule.
_LAST: dict[str, tuple[str, float]] = {}


def _path() -> Path:
    d = Path(str(config_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d / _FILE


def _blocks_path() -> Path:
    d = Path(str(config_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d / _BLOCKS_FILE


def reset() -> None:
    """Drop the process-local quiet state. For tests, and for a role change."""
    _LAST.clear()


def read() -> dict:
    return _io.read_json(_path())


def blocks() -> list[dict]:
    """Every recorded filter block, most recent first."""
    rows = _io.read_json(_blocks_path()).get("blocks") or []
    return list(reversed([r for r in rows if isinstance(r, dict)]))


def _block_counts() -> dict:
    counts: dict[str, int] = {}
    for row in blocks():
        rule = str(row.get("rule") or "")
        counts[rule] = counts.get(rule, 0) + 1
    return counts


def record(*, state: str, admin: str, reachable: bool, group: str = "",
           groups_available: list[str] | None = None, pending: int = 0,
           pushed: int = 0, error: str | None = None) -> dict:
    """Write this cycle's record. Never raises.

    ``last_ok`` is preserved across a failure. "We have not synced since 14:02"
    is the question an operator actually asks, and overwriting the stamp on
    every failed cycle destroys the only thing that can answer it.
    """
    prev = read()
    # Named rather than nested in the dict: "an explicit error wins, a success
    # clears it, and a failure that says nothing keeps the last one we knew" is
    # three rules, and reading them out of a one-line conditional is how the
    # middle one gets dropped.
    if error is not None:
        last_error = error
    elif reachable:
        last_error = None
    else:
        last_error = prev.get("last_error")
    row = {
        "state": state,
        "admin": admin,
        "group": group,
        "groups_available": list(
            groups_available if groups_available is not None
            else (prev.get("groups_available") or [])),
        "reachable": bool(reachable),
        "pending": int(pending),
        "pushed_total": int(prev.get("pushed_total") or 0) + int(pushed),
        "blocked": _block_counts(),
        "last_ok": int(time.time()) if reachable else prev.get("last_ok"),
        "last_error": last_error,
        "at": int(time.time()),
    }
    try:
        _io.write_json(_path(), row)
    except Exception as exc:  # noqa: BLE001 — a status write must not fail a cycle
        _log.info("sync: could not write the status record: %s", exc)
    return row


def record_block(key: str, rule: str, reason: str) -> None:
    """Note that one node was held back. Never raises, never stores the node.

    The KEY and the RULE, never the text. This file is written to disk, and a
    log that records the secret it caught is the leak it was meant to prevent.
    """
    try:
        rows = [r for r in (_io.read_json(_blocks_path()).get("blocks") or [])
                if isinstance(r, dict)]
        rows.append({"key": str(key), "rule": str(rule), "reason": str(reason),
                     "at": int(time.time())})
        _io.write_json(_blocks_path(), {"blocks": rows[-MAX_BLOCKS:]})
    except Exception as exc:  # noqa: BLE001 — bookkeeping is not the payload
        _log.info("sync: could not record a filter block: %s", exc)


def note_failure(admin: str, error: str) -> None:
    """One cycle could not reach ``admin``. Logs only on a change.

    The first failure logs at WARNING. After that the same error is silent until
    ``QUIET_SECONDS`` have passed, at which point one line records that it is
    still down and for how long. A DIFFERENT error logs immediately — an admin
    going from "connection refused" to "401" is news, not more of the same.
    """
    now = time.monotonic()
    seen, at = _LAST.get(admin, ("", 0.0))
    if seen == error and (now - at) < QUIET_SECONDS:
        return
    if seen == error:
        _log.warning("sync: admin %s still unreachable after %.0f minutes: %s",
                     admin, (now - at) / 60.0, error)
    else:
        _log.warning("sync: admin %s is unreachable: %s", admin, error)
    _LAST[admin] = (error, now)


def note_success(admin: str) -> None:
    """One cycle reached ``admin``. Logs only the transition back."""
    if admin in _LAST:
        _log.info("sync: admin %s is reachable again", admin)
        _LAST.pop(admin, None)


__all__ = ["QUIET_SECONDS", "MAX_BLOCKS", "reset", "read", "blocks", "record",
           "record_block", "note_failure", "note_success"]
