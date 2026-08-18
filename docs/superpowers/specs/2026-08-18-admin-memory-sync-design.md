# Admin Memory Sync — Design

**Date:** 2026-08-18
**Status:** Implemented
**Topology:** Hub and spoke. One admin, no mesh, no election.
**Supersedes:** [2026-07-19-p2p-shared-memory-design.md](2026-07-19-p2p-shared-memory-design.md)
and the leader-election half of
[2026-07-20-two-tier-knowledge-compaction.md](2026-07-20-two-tier-knowledge-compaction.md).
The record classes, the identity scheme, the merge rule and the folder layout from
those two are unchanged and are not restated here.

## Problem

The P2P design gave every machine an equal role: full mesh, pull from everyone,
and a leader *derived* from a replicated peer registry so exactly one machine
ran the expensive LLM fold. Making that work needed SSDP to discover peers,
gossip to keep the roster fresh, a shared mesh key to auto-join, a liveness
window to age peers out, and a fallback timer for a leader that answered
manifests but never folded.

Every one of those parts existed to derive a fact the operator already knows:
which box is the always-on one. There is a shared office rig; it is on all the
time and it holds the inference hardware. Naming it costs one environment
variable and deletes the rest.

## Shape

```
 spoke (laptop, NUC, someone else's machine)          admin (the office rig)
 ─────────────────────────────────────────            ──────────────────────
 captures/     my raw pastes    ┐                      captures/     ┐ its own
 md_store.compact               │ local, on EVERY      md_store.compact│ local
 compacted/    my briefs        │ machine              compacted/     │ work
 sync_briefs_to_nodes           │                      …              ┘
 okf/          my knowledge     ┘
        │                                                    │
        └──────────────── push ──────────────────▶  peers/<spoke>/  raw inbox
                                                             │
                                                      tier 1: the merge
                                              (the only CROSS-machine fold)
                                                             │
 mesh/<admin>/ the merge         ◀──────── pull ──────  mesh/<admin>/
 view/  my local tier 2 (okf/ + mesh/)                 view/  its own tier 2
```

A spoke talks to the admin and to nobody else. The admin talks to no one — it
only answers.

**Every machine still compacts its own memory.** Captures → briefs, briefs →
nodes, and `okf/` + `mesh/` → `view/` are all local work on local files, on the
admin and on every spoke alike. The admin's one extra job is the *cross-machine*
merge, because that is the only step whose input is everybody's knowledge at
once.

### Who is the admin

`AIFORGE_ADMIN_URL` decides (`memory/sync/role.py`):

| Config | Role |
|---|---|
| unset | **admin** — which is also what a standalone install is, so a single machine keeps folding with nothing to set |
| `http://rig:8799` | **spoke** |
| `AIFORGE_ROLE=admin\|spoke` | explicit override, for the deployment that needs one |

The admin's *id* is learned, not configured: the admin states it in every
manifest response and the spoke caches it in `$AIFORGE_CONFIG_DIR/admin.json`.
That id is what `okf.tiers` trusts `derived: mesh` nodes from. `AIFORGE_ADMIN_ID`
pins it for an operator who would rather not trust a response.

## Protocol

Four routes, all under `/api/memory/sync/` (`api/routes/sync.py`).

**Up — the spoke pushes** its own OKF nodes and tombstones
(`memory/sync/push.py`):

1. `POST /offer` `{peer, entries[]}` → `{want: [entries]}`. The admin runs the
   same `merge.plan_sync` the pull side runs, with the roles swapped, and
   answers with what it does not already hold. A spoke that has been offline for
   a month therefore costs one small request, not a re-upload of its tree.
2. `POST /push` `{peer, entry, body}` (body base64) → `{applied: bool}`. One
   request per entry, so a single unwritable record costs itself and not the
   batch.

**Down — the spoke pulls** (`memory/sync/loop.py`, unchanged mechanism):

3. `GET /manifest` → `{manifest[], admin, role}`.
4. `GET /blob/{digest}` → the bytes.

Push runs first in a cycle, so the fold at the end of the admin's cycle sees
what the spoke just authored rather than always folding a cycle behind. Both
halves share one `CYCLE_BUDGET`.

### What may travel, and in which direction

| | Up | Down |
|---|---|---|
| `okf/` (own origin) | ✅ the only thing that travels | ❌ |
| tombstones (own origin) | ✅ | ❌ |
| `mesh/<admin>/` (`derived: mesh`) | ❌ refused at the door | ✅ the point of the whole thing |
| `captures/` | ❌ each machine compacts its own | ❌ |
| `compacted/` briefs | ❌ ditto — shipping them would duplicate work already done | ❌ |
| `view/` | never | never |

Because briefs stay put, a fact only reaches the other machines once it is an
**OKF node** — so `okf.author.sync_briefs_to_nodes()` runs on every cycle,
turning this machine's briefs into nodes and *updating* the node when a topic
gains a fact. (The pre-existing `migrate_from_briefs` skips a topic it has
already seen, which is right for a one-shot migration and wrong for a cycle:
every fact learned after the first run would have stayed local forever.) It is
deterministic and costs no tokens — the distillation already happened when the
brief was written.

**No relay.** One spoke's raw node never reaches another spoke. It goes up, is
folded into knowledge the admin authors under *its own* origin, and that fold is
what comes down. This is not a limitation worked around — it is why the hub
exists, and it keeps `apply._accept_class_b`'s origin rule intact: a blob served
by X may only carry `origin: X`, so nothing can speak for a machine that is not
on the other end of the connection.

## Compaction

| Step | Where | Gated? |
|---|---|---|
| `md_store.compact` — captures → briefs | every machine | no |
| `okf.author.sync_briefs_to_nodes` — briefs → nodes | every machine | no |
| `okf.store.dedupe_nodes` — collapse our own near-duplicates | every machine | no |
| `okf.tiers.distil_mesh` — **tier 1**, everybody's knowledge → `mesh/<admin>/` | admin only | `role.may_merge()` |
| `okf.tiers.build_view` — **tier 2**, `okf/` + `mesh/` → `view/` | every machine | no |

Only the cross-machine merge is centralised, and only because its input is
everybody's knowledge at once: two machines folding the same inbox produce two
different answers. Everything else is one machine's own files, shaped by that
machine's own context — which is exactly why tier 2 is not centralised either.

`dedupe_nodes` needs no gate for a structural reason: it can only ever collapse
nodes this machine minted, since `tombstone.mark_deleted` refuses another
origin.

**The merge gate soft-fails OPEN**, as the election gate did, and the reason is
now structural: a machine with no admin url *is* the admin, so there is no state
to read and nothing to fail.

`tiers.view_nodes()` falls back to `mesh/` when `view/` is empty — a machine
that has just pulled a fresh merge but not yet folded it would otherwise read
purely local memory. The two are never read together: `view/` *is* the mesh
folded with local notes, so returning both would double every fact.

## Security

**The sync surface answers with no credential by default** (`api._sync_open`).
That is a deliberate choice for this deployment — the admin sits on a LAN or a
WireGuard address, and spokes should need no secret to keep in step. Three
things bound what that opens:

- **It is scoped.** `AIFORGE_SYNC_AUTH=1` closes it again, and either way the
  control plane — the routes that run shells and write config — still demands
  `AIFORGE_API_TOKEN` from every non-loopback caller. An open sync surface is
  never an open shell.
- **What a spoke may write is bounded by content, not by credential**
  (`memory/sync/inbox.py`): class A is create-only, class B must carry the
  pushing machine's own origin, a node stamped `derived:` is refused outright,
  and both an entry cap and a byte cap apply before anything is buffered.
- **What a spoke may read is bounded too** (`inbox.downstream`): only the fold
  and the briefs are advertised, and a blob is served only if its hash is in
  that list — so another machine's raw notes cannot be read back out of the
  admin by anyone who learns a digest.

What this does *not* defend against: a machine on the same network claiming to
be a spoke it is not. The peer id is self-asserted, so the origin rule is a
consistency check (it stops a misconfigured spoke clobbering another's nodes),
not an authentication boundary. Bind the admin to a trusted interface. Signed
manifests remain the real fix and are still out of scope.

## What was deleted

| Gone | Why |
|---|---|
| `sync/discovery_ssdp.py` | nothing to discover — the admin is named |
| `sync/peers.py` | no roster, no gossip, no `peers.json`, no `AIFORGE_MESH_KEY` |
| `sync/election.py` | the admin is configuration, not a computed fact |
| `GET /api/memory/sync/challenge` | the auto-join handshake it existed for is gone |
| `POST /api/admin/peers` + the peer table | there is one upstream, and it is in the env |
| `AIFORGE_SYNC_SSDP`, `AIFORGE_SYNC_SSDP_HOST`, `AIFORGE_MESH_KEY` | see above |

`transport`, `manifest`, `merge`, `apply`, `paths`, `identity` and `tombstone`
are unchanged in substance: the record format, the identity scheme and the merge
order were never the part that needed a mesh.

## Migration

Nothing on disk changes. A machine that was a peer keeps its `okf/`, its
`peers/` inbox and its `mesh/`; a stale `peers.json` is simply never read again.
On the machine that is to be the admin, do nothing. On every other machine set
`AIFORGE_ADMIN_URL` and drop `AIFORGE_MESH_KEY` / `AIFORGE_SYNC_SSDP`.

A machine that used to fold and is now a spoke retires its own `mesh/<id>/`
subtree on the next cycle and tombstones it (`tiers._retire_own_mesh`), so the
old fold does not ride the sync forever.
