"""Which machine is the admin, and what that machine owes the others.

The mesh is gone. One machine is the **admin**: it receives every other
machine's authored knowledge, merges ACROSS all of it, and serves the merged
result back. Every other machine is a **spoke**: it pushes what it authored and
pulls what the admin merged, and it talks to nobody else.

**Every machine still compacts its own memory.** Turning captures into briefs,
folding its own knowledge into a working view and deduping its own nodes are
local work on local files, and they stay local — a spoke is not a thin client.
The one thing only the admin does is the *cross-machine* merge, because that is
the step whose input is everybody's knowledge at once.

How a machine ends up in one role or the other:

* **``./run.sh --admin``** — the deliberate statement, and the one an operator
  actually types. It exports ``AIFORGE_ROLE=admin`` and PERSISTS that line to
  the env file, so a restart cannot silently demote the box. It is *refused*
  when ``AIFORGE_ADMIN_URL`` is set, rather than overriding it: a machine cannot
  be both, and the flag used to mean something else, so a spoke could otherwise
  be promoted by habit. Exactly one box in a fleet is started this way.
* **``./run.sh --spoke``** — the way back. It drops the persisted line so the
  url decides again; this is how the admin is MOVED to another machine. Without
  it, ``AIFORGE_ROLE`` (which wins over the url) would make the first admin
  permanent: it would stop syncing, keep merging, and never retire its fold.
* **``AIFORGE_ADMIN_URL=http://rig:8799``** — every other machine. It names the
  admin and is therefore a spoke.
* **neither** — a standalone install: nothing to sync with, and it keeps merging
  its own knowledge exactly as it did before any of this existed. This is why
  the default is admin rather than spoke; defaulting the other way would leave a
  lone machine with no merge at all.

``AIFORGE_ROLE`` is the explicit override that ``--admin`` sets, and an operator
may set it by hand for the same reason. It beats ``AIFORGE_ADMIN_URL`` — which
is why run.sh warns when a box holds the admin role *and* names an upstream:
that url is being ignored.

Why no election. The elected-leader design this replaces computed leadership
from a replicated peer registry, which needed discovery to populate the
registry, gossip to keep it fresh, a liveness window to age it out, and a
fallback timer for a leader that answered manifests but never folded. Every one
of those parts existed to *derive* a fact the operator already knows — which box
is the always-on one. Naming it costs one environment variable and deletes all
of them.

**The admin id is learned, not configured.** A spoke must know the admin's peer
id to trust ``mesh/<admin>/`` (``okf.tiers``), but making the operator set it on
every spoke is a second value to keep in step with the first. Instead the admin
states its id in every manifest response and the spoke caches it beside its
other config. ``AIFORGE_ADMIN_ID`` still wins when set, for the operator who
would rather pin it than trust a response.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from aiforge_core.config.paths import config_dir
from aiforge_core.memory.sync import _io

_log = logging.getLogger("aiforge.sync")

ADMIN = "admin"
SPOKE = "spoke"

# Accepted schemes for an admin address. Plain http is deliberate and is the
# documented deployment: the sync surface takes no credential, so the admin is
# expected to sit on a trusted interface — a LAN or a WireGuard address — where
# TLS buys nothing that binding correctly has not already bought. Assembled
# rather than written as one literal so the scheme list is stated once.
_SCHEMES = tuple(f"{s}://" for s in ("http", "https"))


def admin_url() -> str:
    """Base url of the admin, or "" when this machine is the admin.

    ``AIFORGE_ADMIN_URL`` wins, then the value saved from the settings screen.
    The env var stays authoritative so an operator who pins it in ``.env`` (or
    passes ``./run.sh --admin-url``) cannot have it silently overridden by a
    click; the saved value is for the machine where nobody edits files.

    A trailing slash is stripped here, once, because every caller concatenates a
    path onto it and ``//api/...`` is a 404 on some proxies and a redirect on
    others.
    """
    env = (os.environ.get("AIFORGE_ADMIN_URL") or "").strip()
    if env:
        return env.rstrip("/")
    return str(_io.read_json(_state_path()).get("url") or "").strip().rstrip("/")


def set_admin_url(url: str) -> str:
    """Save the admin this machine syncs with. Returns what is now in effect.

    Refused while ``AIFORGE_ROLE=admin`` is set, for the reason ``run.sh``
    refuses ``--admin-url`` on the same box: a machine that is both stamps
    ``derived: mesh`` while also pushing to somebody else's hub, so knowledge
    crosses in both directions and two machines claim the same fold.

    An empty string clears it, which hands the decision back to the env var —
    and, with neither set, makes this machine the admin again.
    """
    url = (url or "").strip().rstrip("/")
    if url and (os.environ.get("AIFORGE_ROLE") or "").strip().lower() == ADMIN:
        raise ValueError(
            "this machine holds the admin role (AIFORGE_ROLE=admin), so it "
            "cannot also be a spoke — run ./run.sh --spoke here first")
    if url and not url.startswith(_SCHEMES):
        # A bare host is the commonest thing to type, and it fails later as an
        # unreachable admin rather than as the typo it is.
        raise ValueError(f"{url!r} must start with http:// or https://")
    rec = dict(_io.read_json(_state_path()))
    if url:
        rec["url"] = url
    else:
        rec.pop("url", None)
    _io.write_json(_state_path(), rec)
    _log.info("sync: admin is now %s", url or "(unset)")
    return admin_url()


def role() -> str:
    """``admin`` or ``spoke``. ``AIFORGE_ROLE`` wins; otherwise the url decides.

    An unrecognised ``AIFORGE_ROLE`` is ignored rather than fatal: this is read
    on every sync cycle and from inside the compaction gate, and a typo must not
    be the thing that stops a daemon whose whole design is to outlive bad state.
    """
    explicit = (os.environ.get("AIFORGE_ROLE") or "").strip().lower()
    if explicit in (ADMIN, SPOKE):
        return explicit
    if explicit:
        _log.warning("sync: AIFORGE_ROLE=%r is neither %s nor %s — ignoring it "
                     "and deciding from AIFORGE_ADMIN_URL", explicit, ADMIN, SPOKE)
    return SPOKE if admin_url() else ADMIN


def is_admin() -> bool:
    return role() == ADMIN


def may_merge() -> bool:
    """Whether this machine may run the CROSS-MACHINE merge (tier 1).

    The admin, and only it: merging everybody's knowledge is LLM-expensive and
    non-deterministic, so two machines folding the same inbox produce two
    different answers. Local compaction is NOT gated by this — every machine
    distils its own captures and builds its own view.

    Soft-fails OPEN, like the election gate it replaces: a standalone install
    has no admin url, is therefore the admin, and must keep merging. There is no
    state to read and nothing to fail, so the only way to answer False is to be
    a configured spoke.
    """
    return is_admin()


def _state_path() -> Path:
    """Where the learned admin id is cached.

    Config, not memory: it lives beside the other config files (the directory
    ``peers.json`` used to live in) and is never synced. A spoke that loses it
    re-learns it from the admin's next manifest response.
    """
    d = Path(str(config_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d / "admin.json"


def remember_admin_id(value: str) -> str:
    """Cache the id the admin stated in its manifest response. Returns what is
    now held, which is the pinned value when one is pinned.

    Written only on a change, so an ordinary cycle costs a read and no write.
    A value that is not addressable is dropped rather than stored: it would
    become a directory component under ``mesh/`` and every consumer folds it.
    """
    from aiforge_core.memory.sync import paths

    pinned = (os.environ.get("AIFORGE_ADMIN_ID") or "").strip()
    if pinned:
        return paths.fold(pinned)
    slug = paths.fold(value)
    if not value or not paths.is_addressable(slug):
        return admin_id()
    rec = dict(_io.read_json(_state_path()))
    if slug != str(rec.get("id") or ""):
        # MERGED, not replaced. This file also holds the admin url saved from
        # the settings screen, and writing ``{"id": ...}`` over it dropped that
        # url on the first cycle after it was set — the machine forgot its admin
        # the moment it learned who the admin was.
        rec["id"] = slug
        _io.write_json(_state_path(), rec)
        _log.info("sync: admin is %s", slug)
    return slug


def admin_id() -> str:
    """The admin's peer id in comparison form, or "" when it is not known yet.

    On the admin itself this is always our own id — asking a machine who its
    admin is, when it *is* the admin, must never depend on a cached response it
    never received.
    """
    from aiforge_core.memory.sync import identity, paths

    pinned = (os.environ.get("AIFORGE_ADMIN_ID") or "").strip()
    if pinned:
        return paths.fold(pinned)
    if is_admin():
        return paths.fold(identity.self_id())
    return str(_io.read_json(_state_path()).get("id") or "")


__all__ = ["ADMIN", "SPOKE", "admin_url", "set_admin_url", "role",
           "is_admin", "may_merge",
           "admin_id", "remember_admin_id"]
