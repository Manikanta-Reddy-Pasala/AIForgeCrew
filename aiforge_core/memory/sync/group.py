"""Groups: one admin, several independent fleets.

The design this extends gave one admin one pool of knowledge — a fleet was
"every machine that names this admin". An operator running more than one pool
(different customers, different sites) wanted one hub box rather than one per
pool, and a group is the smallest thing that buys it: **a name the admin
publishes and a client selects**.

**The admin owns the list; the client learns it.** The alternative — each client
naming its own group — was rejected in design: a typo silently creates a second
pool that looks like a working sync (the client pushes happily, the admin
accepts happily) and nobody notices until somebody asks why two machines cannot
see each other's knowledge.

**No group name is hardcoded anywhere.** An admin with no groups configured runs
*ungrouped*, which is byte-for-byte the behaviour of the design this extends, so
every existing install keeps working with no configuration and no migration.

**A group is not a security boundary.** It has no key. A client states its group
and is believed, exactly as it states its peer id and is believed (see
``inbox``). The check is a routing and consistency rule: it stops a
misconfigured client writing into the wrong pool, not a hostile one on the same
network. Bind the admin to a trusted interface; see the security posture section
of ``docs/superpowers/specs/2026-08-26-group-memory-sync-design.md``.
"""
from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

from aiforge_core.config.paths import config_dir
from aiforge_core.memory.sync import _io, paths

_log = logging.getLogger("aiforge.sync")

# States ``resolve`` can report. Mirrored into ``status.state``, so the settings
# screen and the cycle agree on the vocabulary rather than each inventing one.
OK = "ok"
# Reported when nothing was chosen and the admin's default was taken instead.
# A distinct state, not an error: sync proceeds, and the settings screen says
# which group it landed in so an operator can move it.
DEFAULTED = "group-defaulted"
UNKNOWN = "group-unknown"

# Where each side keeps its half. Config, not memory: both describe this
# deployment rather than knowledge, and neither may ever sync.
_LIST_FILE = "groups.json"          # the admin's published list
_CHOICE_FILE = "sync_group.json"    # the client's selection

# The directory every group's tree hangs off, below the memory root.
GROUPS_DIR = "groups"


def is_valid(name: str) -> bool:
    """True when ``name`` may be a group.

    A group name becomes a directory component under ``groups/``, so it takes
    the identity alphabet ``paths.is_addressable`` already owns — the same rule
    that guards a peer id, including its length cap. A name that does not
    round-trip is refused rather than repaired: repairing invents a group the
    operator never asked for, and two different bad names repair onto one.
    """
    return paths.is_addressable(str(name or ""))


def _list_path() -> Path:
    d = Path(str(config_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d / _LIST_FILE


def _choice_path() -> Path:
    d = Path(str(config_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d / _CHOICE_FILE


def default_of(rows: list[str]) -> str:
    """The group a client joins when nobody has chosen one: the FIRST published.

    The admin's list is ordered by creation, so the first entry is the one the
    operator set up first — the main pool on every deployment that has a main
    pool. Making it the default is what lets a new machine sync out of the box
    with nothing configured on it at all.

    The alternative, refusing to sync until somebody picks, was tried first and
    is safer in one narrow way: knowledge cannot land in a pool nobody intended.
    It is worse in the way that actually bites, though — a machine that quietly
    syncs nothing looks exactly like a machine that is syncing fine. Defaulting
    is visible (the settings screen names the group and offers the others) and
    reversible (``snapshot``), and it fails loudly rather than silently.
    """
    return rows[0] if rows else ""


def known() -> list[str]:
    """The groups this admin publishes, in creation order. ``[]`` = ungrouped.

    Seeded from ``AIFORGE_SYNC_GROUPS`` only while no file exists, so an
    operator who has since created groups through the API is not overwritten by
    a stale line in an env file — once the file exists it is the record.
    """
    rows = _io.read_json(_list_path()).get("groups")
    if isinstance(rows, list):
        return [str(g) for g in rows if is_valid(str(g))]
    seed = (os.environ.get("AIFORGE_SYNC_GROUPS") or "").split(",")
    return [g for g in (s.strip() for s in seed) if is_valid(g)]


def create(name: str) -> list[str]:
    """Publish ``name``. Idempotent. Returns the whole list."""
    if not is_valid(name):
        raise ValueError(
            f"{name!r} is not a usable group name: it becomes a directory "
            "component, so it takes the [A-Za-z0-9_-] identity alphabet")
    rows = known()
    if name not in rows:
        rows = [*rows, name]
        _io.write_json(_list_path(), {"groups": rows})
        _log.info("sync: published group %s", name)
    return rows


def selected() -> str:
    """This client's group: the pin, else the cached choice, else "".

    An unusable pin is ignored rather than fatal — this is read on every cycle,
    and a typo in an env file must not be the thing that stops a daemon whose
    whole design is to outlive bad state. Discovery then decides.
    """
    pinned = (os.environ.get("AIFORGE_SYNC_GROUP") or "").strip()
    if pinned:
        if is_valid(pinned):
            return pinned
        _log.warning("sync: AIFORGE_SYNC_GROUP=%r is not a usable group name — "
                     "ignoring it and letting discovery decide", pinned)
        return ""
    return str(_io.read_json(_choice_path()).get("group") or "")


def choose(name: str) -> str:
    """Persist this client's selection. Written only on a change."""
    if not is_valid(name):
        raise ValueError(f"{name!r} is not a usable group name")
    if name != str(_io.read_json(_choice_path()).get("group") or ""):
        _io.write_json(_choice_path(), {"group": name})
        _log.info("sync: joined group %s", name)
    return name


def resolve(advertised: list[str]) -> tuple[str, str]:
    """``(group, state)`` for this cycle, given what the admin advertises.

    Order, highest first:

    1. ``AIFORGE_SYNC_GROUP`` — the operator pinned it, and discovery is not
       consulted. A pinned group the admin does not advertise is still used: the
       operator knows something the cycle does not, and the admin answers 404 if
       they are wrong, which is a better failure than silently syncing
       somewhere else.
    2. A cached choice. Kept even when it vanishes from the list — see below.
    3. Exactly one advertised group: select it and persist. This is the
       single-group deployment, and it needs no UI at all.
    4. Several advertised and none chosen: the admin's DEFAULT (the first it
       publishes), persisted, and reported as ``DEFAULTED`` so the settings
       screen can say which group it landed in and offer the others.
    5. None advertised: ungrouped, the legacy behaviour.

    A cached choice that disappears from the list is **kept** and reported as
    ``UNKNOWN``, never cleared. Clearing it would re-run the auto-select in rule
    3 and move this machine's knowledge into a different pool because somebody
    was mid-edit on the admin.
    """
    pinned = (os.environ.get("AIFORGE_SYNC_GROUP") or "").strip()
    if pinned and is_valid(pinned):
        return pinned, OK

    rows = [g for g in (advertised or []) if is_valid(str(g))]
    chosen = selected()
    if chosen:
        # An ungrouped admin (no rows at all) is not evidence the choice is
        # wrong — it may simply have dropped its last group — so only a
        # NON-EMPTY list that excludes the choice reports UNKNOWN.
        return chosen, (UNKNOWN if rows and chosen not in rows else OK)
    if len(rows) == 1:
        return choose(rows[0]), OK
    if len(rows) > 1:
        # The admin's default, taken rather than refusing to sync — see
        # ``default_of``. Persisted, so this decision is made once and is then
        # an ordinary chosen group like any other.
        return choose(default_of(rows)), DEFAULTED
    return "", OK


@contextlib.contextmanager
def scoped(name: str):
    """Point the memory tree at ``name``'s subtree for the duration.

    This is the whole of group isolation. Every module below ``_io.root()`` —
    ``paths``, ``manifest``, ``merge``, ``apply``, ``inbox``, ``tiers`` — is
    already written against that one function, so scoping it scopes all of them
    and none of them needs to learn what a group is.

    An empty name is a no-op, because ungrouped is a real deployment rather than
    an error state, and every caller would otherwise need the same branch.
    """
    if not name:
        yield
        return
    if not is_valid(name):
        raise ValueError(f"{name!r} is not a usable group name")
    token = _io.push_scope(_io.root() / GROUPS_DIR / name)
    try:
        yield
    finally:
        _io.pop_scope(token)


__all__ = ["OK", "DEFAULTED", "UNKNOWN", "GROUPS_DIR",
           "is_valid", "known", "default_of", "create", "selected",
           "choose", "resolve", "scoped"]
