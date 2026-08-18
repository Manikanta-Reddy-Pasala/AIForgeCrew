# Two-Tier Knowledge Compaction — Design

**Date:** 2026-07-20
**Status:** Implemented; the leader-election half SUPERSEDED (2026-08-18) by
[2026-08-18-admin-memory-sync-design.md](2026-08-18-admin-memory-sync-design.md) —
the folding machine is now named by ``AIFORGE_ADMIN_URL`` rather than elected, and
tier 2 runs on that machine only (a spoke reads ``mesh/`` as its view). The two-tier
shape, the folder layout and the amplification rules below are unchanged.
**Supersedes:** the `okf/peers/<origin>/` layout in
`2026-07-19-p2p-shared-memory-design.md` (schema change 1)

## Problem

Peer sync currently lands foreign OKF nodes at `okf/peers/<origin>/<key>.md` —
*inside* the tree that compaction reads as its source. Two consequences:

1. **`okf/` stops meaning "my knowledge."** It becomes the union of every peer's
   raw nodes, so each machine's compaction sees a different pile of inputs and
   produces a different result from the same mesh.
2. **Every peer redoes the same distillation.** The expensive, non-deterministic
   work — merging near-duplicate knowledge across machines — happens N times and
   converges N different ways.

The lease was meant to stop (2) by electing one compactor, but it elects nobody
useful (its TTL is shorter than the sync interval, so a replicated lease is
always expired on arrival — see the election fix). Even with a working election,
(1) remains: a single compactor still cannot produce a result that suits every
peer, because each peer's local context differs.

## Shape

Two tiers, each with exactly one owner.

**Tier 1 — the leader, once per mesh.** Every peer's authored knowledge flows in
by ordinary sync and lands in an inbox. The leader distils across all of it,
grouped by topic/repo, and produces one compacted OKF representing mesh-wide
knowledge. That result syncs out to everyone.

**Tier 2 — each peer, locally.** The mesh result lands *beside* the peer's own
`okf/`, never overwriting it. The peer compacts its own `okf/` together with the
mesh folder to produce its working view.

The expensive cross-peer merge happens once. The cheap local merge happens per
machine, on a small input, and produces knowledge shaped by that machine's own
context.

## Folder layout

Four directories under `memory_dir()`, each with one writer:

| Directory | Written by | Synced | Purpose |
|---|---|---|---|
| `okf/` | this machine (agents, chat, local compaction) | **up** — advertised in the manifest | My own authored knowledge. The only thing this peer contributes to the mesh. |
| `peers/<origin>/` | sync applier | **in** — received from peers | Raw inbox of other peers' authored nodes. Never edited locally. Not a compaction source for tier 2. |
| `mesh/` | the leader only | **down** — from the leader | The tier-1 global compacted OKF. A read-only mirror on non-leaders. |
| `view/` | this machine's tier-2 compaction | **never** | The working view: `okf/` merged with `mesh/`. Local-only by construction. |

`okf/peers/` disappears. A migration moves any existing `okf/peers/<origin>/*` to
`peers/<origin>/*`.

The `derived: mesh` marker travels in the manifest, and an arriving node that
carries it is filed in `mesh/` rather than in the raw inbox — otherwise `mesh/`
would stay empty on every follower and the per-directory counts would mislead.
An identity already held is still updated where it lives.

**What agents read:** `okf/` (own raw knowledge) plus `view/` (distilled mesh +
own). `mesh/` is an *input* to `view/`, not read directly by retrieval — reading
both would surface the same content twice.

## The feedback loop, and how it is broken

If tier-2 output were written into `okf/`, it would sync upward, the leader would
fold it into `mesh/`, `mesh/` would come back down, and it would be merged again.
Knowledge amplifies and drifts slightly on every round — a failure that looks
fine for days before the notes start reading strangely.

**The break: tier-2 output is local-only and never syncs.** It lives in `view/`,
which is excluded from the manifest exactly as `.conflict.md` sidecars and
`index.md` already are. Only genuinely new *local authorship* in `okf/` travels
upward.

Two supporting rules:

- Nodes in `mesh/` carry a `derived: mesh` marker. The leader ingests only peer
  nodes lacking it, so a peer that somehow republishes mesh content cannot feed
  it back into tier 1.
- `view/` is regenerated, never merged into. It can be deleted at any time and
  rebuilt from `okf/` + `mesh/`.

## Triggering

- **Tier 1** runs on the leader's ordinary 30-minute sync cycle. One moving part,
  not a second schedule.
- **Tier 2** runs on a peer only when `mesh/` actually changed since its last
  view build — no LLM spend on a cycle where nothing arrived. Staleness is
  detected with the same fingerprint approach `manifest.build()` already uses
  (file count, total size, newest mtime).
- Grouping in both tiers reuses the topic/repo grouping `compact()` already
  implements. No second grouping concept.

## Who is the leader

Deterministic election from the replicated roster (see the election fix landing
alongside this): leader is the lexicographically smallest id among this peer and
every approved peer seen within the alive-window. No wall-clock comparison across
machines — `last_seen` is written by our own clock observing them.

With no approved peers a machine is trivially the leader, so a single-machine
install runs tier 1 over an empty inbox and behaves exactly as it does today.

## Failure behaviour

| Situation | Behaviour |
|---|---|
| Leader offline | No new `mesh/`. Peers keep their last one and keep authoring into `okf/`. Nothing blocks. Next leader takes over when `last_seen` ages out. |
| Two peers both believe they lead | Both produce a `mesh/`. Both are content-addressed; the merge rules pick one deterministically and the next dedupe pass folds the rest. Wasted tokens, never corruption — unchanged from the existing design. |
| `mesh/` arrives corrupt/unparseable | Tier 2 skips and keeps the previous `view/`. A bad mesh must never destroy a good local view. |
| Election check raises | Distil anyway. Losing compaction entirely is worse than a duplicate. |
| Covered-set check raises | Archive nothing. An un-archived capture is untidy; an archived-but-undistilled one is unrecoverable. |
| Peer never syncs | Keeps working entirely locally; `okf/` and `view/` stay valid. |

## Cost

Tier 2 means every peer spends some tokens whenever the mesh updates. The input
is its own `okf/` plus one mesh folder — not N peers' raw material — and it is
skipped entirely when both of those inputs are unchanged (`okf/` counts because
a note authored here must reach the local view without waiting on the leader).
Accepted: the local view is what makes
shared knowledge usable on a machine whose context differs from the leader's.

## Out of scope

Per-peer privacy filtering (everything in `okf/` syncs to every approved peer),
encryption at rest, and any push-based transport. The mesh stays pull-only.
