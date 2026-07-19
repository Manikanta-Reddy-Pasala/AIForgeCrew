# P2P Shared Memory — Design

**Date:** 2026-07-19
**Status:** Design settled, ready to plan
**Topology:** Full mesh, pull-only, no master for replication

## Problem

Multiple AIForgeCrew instances run on different machines — some mine (Mac Studio, NUC,
laptop), some belonging to other people. Each keeps its own local memory. A fact learned on
one instance is invisible to the others.

Goal: every peer converges on a shared body of memory without a central server, without a
coordinator, and without any peer being load-bearing.

### In scope

- Memory notes and OKF facts (`okf/**`)
- Captures and compacted briefs (`captures/`, `compacted/`)
- Failure memory, repo standards, learnings

### Out of scope

Chat sessions, tickets, pipeline state, repo indexes, media, and vectors. Vectors are
explicitly excluded: peers may run different embedding backends (`AIFORGE_EMBED_BACKEND`), so
a transferred vector could be meaningless or the wrong dimension. `memory.db` never crosses
the wire — it is rebuilt locally from the markdown that does.

## Two facts that shape everything

**Markdown is the source of truth; SQLite is a derived index.** The store is a tree of small
text files under `memory_dir()` (`aiforge_core/memory/md_store/_base.py:32`) — `captures/`,
`compacted/`, and the OKF bundle at `okf_root()` (`aiforge_core/memory/okf/store.py:52`).
`memory.db` (`aiforge_core/memory/sqlite_memory/_schema.py:154`) is rebuilt from that tree.
Syncing therefore means replicating text files. No database replication, no WAL shipping.

**Files are already content-addressed.** Filenames carry `sha1(title + text)[:6]`
(`aiforge_core/memory/md_store/_ingest.py:72`). Two peers that independently learn the same
thing produce byte-identical filenames. Deduplication is free, and a grow-only set of such
files is a CRDT — every peer converges regardless of arrival order, duplication, or downtime.

What remains is the small mutable subset. That subset is the entire design.

## Architecture

### Two classes of record

| Class | Files | Mutable | Merge rule |
|---|---|---|---|
| A — immutable | `captures/*.md`, `compacted/*.md` | No | Union by content hash |
| B — mutable | `okf/**/*.md`, failure memory, repo standards | Yes | Last-writer-wins per `(origin, key)`, ordered by `rev` |

Class A needs no new code beyond transport. Class B needs the two schema changes below.

### Schema change 1 — identity is a compound key

OKF ids are minted today as per-scope counters — `O-01`, `KR-01`, `L-01`
(`aiforge_core/memory/okf/store.py:127` `next_id()`). Two peers each mint `O-01` for
unrelated objects.

Identity becomes the pair `(origin, key)`. Ids on disk are **not rewritten**; the node gains
an `origin` field naming the peer that minted it.

```
(nuc, O-01)  ≠  (ms, O-01)  ≠  (alice, O-01)
```

A link naming a bare `O-01` resolves against its containing node's `origin`. A link may name
a foreign scope explicitly. Every lookup, link resolution and index key carries the scope
prefix from here on — that is the accepted cost of avoiding a migration.

**Identity is compound, but the filename is not.** `_filename()`
(`aiforge_core/memory/okf/store.py:115`) renders both `(nuc, O-01)` and `(ms, O-01)` to
`O-01.md`, so two peers' unrelated nodes would collide on disk. The receiver therefore
computes the local target path itself and treats the sender's advertised `path` as a hint
only:

- an identity already held locally is updated in place, wherever it currently lives;
- anything new minted by another peer lands under `okf/peers/<origin>/<key>.md`.

Every peer derives the same answer from the same inputs, so the on-disk layout converges along
with the content. Class A is exempt — capture filenames already embed a content digest and are
globally unique.

### Schema change 2 — version stamp on mutable nodes

Three frontmatter fields:

```yaml
---
type: learning
key:  L-07
origin:     nuc     # who minted it — half of the identity
rev:        47      # +1 on every local write
updated_by: ms      # who wrote rev 47
---
```

Merge compares `(rev, updated_by)`: higher `rev` wins, ties break on peer slug
(lexicographic).

**Ordering uses a counter, never wall-clock time.** Peers include other people's machines
whose clocks will disagree, sometimes badly. A timestamp-based LWW would hand every conflict
to the most wrong clock in the mesh.

Both changes land in `render_note` / `parse_note`
(`aiforge_core/runtime/work_notes/_render.py:89,161`), which is already the single choke point
every note write passes through, and in `okf/nodes.py` `render_node()` for OKF frontmatter.

### Protocol — pull-only anti-entropy

Each peer exposes two read-only endpoints on the API server it already runs
(`aiforge_core/api/api.py:90`, routes alongside `aiforge_core/api/routes/memory.py`):

```
GET /api/memory/sync/manifest
    → {
        manifest: [{path, hash, kind, origin?, key?, rev?, updated_by?}, …],
        roster:   [{id, urls[], last_seen}, …]
      }

GET /api/memory/sync/blob/{hash}
    → raw file bytes
```

Sync loop, every 15 minutes by default, once per approved peer:

1. Fetch the peer's manifest.
2. Diff against the local manifest. Want any class A entry whose hash is absent locally; want
   any class B entry — matched on `(origin, key)` — whose `(rev, updated_by)` beats the local
   copy.
3. Fetch each wanted blob, verify its hash against the advertised value, write to a temp file
   and `os.replace` — matching the atomicity already used by `okf/store.py:162` `save_node()`.
4. Ingest changed files into the local index through the existing write path
   (`aiforge_core/memory/md_store/_ingest.py:54` `write()`).

**Pull only. Never push.** No sessions, no handshake, no delivery guarantees, no retry queues.
Every node pulling from every other node is sufficient for the whole mesh to converge. A peer
that is down is simply a request returning nothing this cycle.

Manifest responses are gzipped. Peer count is expected under 20 and the manifest is a few
thousand rows of JSON; no incremental-digest or Merkle optimisation until measurement says
otherwise.

### Deletes — tombstones

Union merge cannot express removal; the next pull restores the file. Deletion writes a
tombstone instead:

```
okf/.tomb/<origin>/<key>.json   →   {origin, key, rev, updated_by, tomb: true}
```

The tombstone is itself a class B record and merges by the same rule. A delete at `rev` 48
beats an edit at 47; an edit at 49 resurrects the node. Tombstones are reaped after 90 days.
A peer offline longer than that must full-resync — an acceptable price for not carrying
tombstones forever.

### Conflicts — `.conflict` sidecar

When two peers edit the same `(origin, key)` before either syncs, LWW picks a winner and the
loser's text is written beside it as a `.conflict` sidecar rather than discarded. The next
compaction pass surfaces both versions and folds them. Sidecars are reaped on the same 90-day
clock as tombstones.

**Sidecars are local artefacts and are never synced.** They are excluded from the manifest.
Each peer generates its own when its own merge discards a version, so replicating them would
multiply the same conflict across the mesh.

Rationale: concurrent edits are rare, but real work vanishing without trace is the kind of
failure that erodes trust in the whole memory.

### Discovery

**Seed.** `peers.json` ships with at least one reachable peer. Every P2P system needs a
bootstrap; this is one line.

**Gossip.** The roster rides along on the manifest response already being fetched each cycle.
Pull from `nuc`, learn about `alice` and `bob` for free. No tracker, no new port, no new
mechanism. The roster is eventually consistent and is allowed to be wrong — being wrong costs
one failed request.

**Discovery is not trust.** This is the load-bearing rule. A gossiped peer lands in
`candidates`: visible, never pulled from. Promotion requires a token obtained out-of-band from
that human. The roster carries ids and urls only, **never tokens**. A compromised peer can
spam your candidate list; it cannot add itself to your mesh.

**Reachability.** Each peer advertises an ordered url list — WireGuard address, LAN address,
public HTTPS. Try in order, cache whichever answered, re-probe on failure. This covers a mesh
of private machines and internet-reachable machines without a NAT-traversal story.

**SSDP, local segment only.** On a flat LAN the seed url can be skipped. SSDP — HTTP-shaped
text over UDP multicast to `239.255.255.250:1900` — provides announce and search in roughly
sixty lines on a raw socket with no dependency. Its `LOCATION` header already carries a url
and `CACHE-CONTROL: max-age` already ages entries out. Chosen over mDNS/DNS-SD because it
needs no record marshalling and no library.

```
M-SEARCH * HTTP/1.1
HOST: 239.255.255.250:1900
MAN: "ssdp:discover"
ST: urn:aiforge:service:memory-sync:1

→ LOCATION: http://10.0.1.14:8799/api/memory/sync/manifest
→ USN: uuid:nuc::urn:aiforge:service:memory-sync:1
```

SSDP cannot be the primary mechanism. Multicast is link-local: small TTL, dropped by routers
and access points. WireGuard is a routed L3 tunnel with no broadcast domain, so multicast does
not traverse it — SSDP fails between my *own* machines once they talk over `wg1`, not merely
across the internet. Docker bridges and most corporate or guest wifi filter it too. SSDP saves
typing a seed url when two peers share a physical segment; gossip carries the mesh.

Two cautions: SSDP is unauthenticated and trivially spoofable, harmless here only because
discovered peers land in candidates and need a hand-supplied token regardless; and SSDP
responders are a known DDoS amplification vector, so the socket binds to a specific LAN
interface, never `0.0.0.0`.

**Departure is passive.** No leave message. `last_seen` ages; a peer unreachable for 30 days
is archived out of the roster and reappears on its next successful contact.

### Trust

Peers live in `$AIFORGE_CONFIG_DIR/peers.json` as `{id, urls[], token, state, last_seen}`
where `state` is `approved` or `candidate`. The token is a bearer credential on both
endpoints. This file is local configuration, not memory: it is never synced and never appears
in the manifest. The gossiped roster is merged *into* it, subject to the candidate rule below.

Two implementation notes settled during planning. The registry is JSON rather than YAML
because PyYAML is not a root dependency and every config file in this repo is JSON through the
`aiforge_core/config/integrations.py:15` idiom. And the token requires no new code:
`aiforge_core/api/api.py:581` already enforces `AIFORGE_API_TOKEN` as bearer auth on every
path beginning `/api/`, which the sync routes do.

Because the surface is read-only and the direction is pull-only, a hostile peer's blast radius
is narrow. It cannot delete local data, cannot overwrite a node with a stale revision, and
cannot force a fetch — the puller chooses what it wants. Blob hashes are verified on arrival,
so it cannot serve different bytes than it advertised.

Residual risk, stated plainly: a trusted peer can feed wrong *facts*. No protocol fixes that.
It is a policy question, and the policy is already set by choosing to share a memory with
those people. Revocation is removing the token from `peers.json`; the peer's class B nodes
stop winning merges and its class A files can be purged by hash.

Deferred: an ed25519 keypair per peer with `peer_id = hash(pubkey)` and signed manifests would
make roster entries self-certifying and allow verifying a never-contacted peer. Not needed
while promotion stays manual.

### Leader — compaction only

Replication needs no leader. Three operations do, because they are LLM-expensive and
non-deterministic: compaction, OKF node deduplication
(`aiforge_core/memory/okf/store.py:451` `dedupe_nodes()`), and distillation. Two peers running
them concurrently produce different answers from the same input.

The lease is a class B record — `okf/.lease` holding `{holder, rev, expires_at}` — and syncs
by the same rule as everything else. It is the one class B record with no `origin`: it is a
mesh-wide singleton, so its identity is the fixed path and it merges on `rev` alone, with the
holder slug as tiebreak.

- **Claim.** If the lease is absent or expired, write it with `rev + 1`. Wait one full sync
  interval, then read again. Still holding it? You are the leader. *That wait replaces
  consensus.*
- **Renew** every 3 minutes. **TTL** 10 minutes.
- **Leader dies.** The lease lapses on its own; the next peer to notice claims it. No failure
  detection, no failover protocol, no quorum.

**Split-brain is tolerated by design.** If two peers both believe they hold the lease, both
compact. Both briefs are class A content-addressed files, so both land, and the next
concept-similarity dedupe pass merges them. The cost of split-brain is wasted tokens, never
corruption — which is precisely why this does not need Raft.

The lease is the only component that reads a wall clock. Its failure mode under skew is
duplicate compaction, which is already tolerated.

## Failure handling

| Failure | Behaviour | Impact |
|---|---|---|
| Peer unreachable | Skip, retry next cycle. Pull-only means nothing blocks on it. | None |
| Peer permanently dead | Nothing to do. Its data is already at every peer that pulled from it. | None |
| New peer joins | Empty local manifest means the diff is everything. Bootstrap *is* the steady-state path. | None |
| Interrupted fetch | Per-blob, hash-verified, atomic rename. A failed blob reappears in the next diff. | None |
| Network partition | Both sides keep working and diverge; revision LWW converges them on heal. | None |
| Clock skew | Irrelevant to merge — ordering is `(rev, peer)`. Only the lease reads a clock. | Lease only |
| Concurrent edit, same node | Higher `rev` wins, ties on peer slug. Loser preserved as `.conflict` sidecar. | Needs review |
| Corrupt or mismatched blob | Hash check fails; blob discarded, logged, retried next cycle. | None |
| Leader dies mid-compaction | Lease lapses after 10 min; next peer claims and redoes it. Partial output is content-addressed, so it is complete or absent. | ≤10 min lag |
| Two leaders at once | Both compact. Duplicate briefs merged by concept-similarity dedupe. | Wasted tokens |
| All seed peers down | Partitioned, not broken. Local work continues; converges when any peer answers. | Stale until heal |
| Peer changes address | Url list tried in order; roster entry updates on next successful contact. | None |
| Roster poisoned with fake peers | They land in `candidates`, never pulled from. Promotion needs a hand-supplied token. | Noise only |
| Malicious peer serves bad facts | Not a protocol failure. Revoke the token; its nodes stop winning, its files purge by hash. | Policy |

## Components

Each unit has one job and a testable boundary.

| Unit | Responsibility | Depends on |
|---|---|---|
| `sync/identity.py` | This peer's slug; the `origin`/`rev`/`updated_by` stamp | nothing |
| `sync/manifest.py` | Build the local manifest from the memory tree | `md_store`, `okf.nodes` |
| `sync/merge.py` | Given local + remote manifest entries, decide want/keep/conflict | nothing (pure) |
| `sync/client.py` | Fetch from one peer, verify, resolve the local target, write atomically | `okf.nodes` |
| `sync/tombstone.py` | Express a local delete as a record the mesh can merge | `client` |
| `api/routes/sync.py` | Serve the two endpoints (bearer auth inherited from `/api/`) | `manifest`, `peers` |
| `sync/peers.py` | `peers.json` load/save, roster gossip merge, candidate quarantine | `identity` |
| `sync/discovery_ssdp.py` | Multicast announce/search on the local segment | `peers` |
| `sync/lease.py` | Claim, renew, check the compaction lease | `okf.store` |
| `sync/loop.py` | Scheduler that runs the cycle per peer | all of the above |

`merge.py` is deliberately pure — it takes two lists and returns a decision set with no I/O.
That is where the interesting logic lives, and it must be testable without a network, a
filesystem, or a second machine.

## Testing

- **Merge rules (unit, pure).** `(rev, updated_by)` ordering including ties; tombstone beats
  older edit; newer edit beats tombstone; conflict detection produces a sidecar decision. No
  I/O.
- **Manifest round-trip (unit).** Build a manifest from a fixture tree, confirm class
  assignment and that class A entries carry a hash matching the filename digest.
- **Two-peer convergence (integration).** Two memory dirs and two in-process API apps. Write
  disjoint notes on each, run one cycle both directions, assert both trees are identical.
  Then write *conflicting* edits to one `(origin, key)` and assert the winner plus sidecar.
- **Three-peer transitive discovery (integration).** A knows B, B knows C. After two cycles A
  has C as a `candidate` and — critically — has **not** pulled from it.
- **Partition and heal (integration).** Diverge two peers with the network stubbed out,
  reconnect, assert convergence and that no write was lost.
- **Lease (integration).** Two peers race to claim; assert exactly one holds after the
  claim-wait-verify sequence. Then stall the holder past TTL and assert the other claims.
- **Hash verification (unit).** A server that returns bytes not matching the advertised hash
  must have its blob rejected and not written.
- **Idempotence.** Running a full cycle twice changes nothing on the second pass.

## Decisions on the record

**Identity is the compound key `(origin, key)`.** Rejected: rewriting every id to `O-nuc-01`.
Buys zero migration — existing notes are already correct and merely gain an `origin` field.
Costs a scope prefix on every lookup, link and index key from now on.

**A losing edit is kept as a `.conflict` sidecar.** Rejected: dropping it silently. Buys never
losing work. Costs stray files and occasional surprise; reaped on the tombstone clock.

**Discovery gossips over the manifest; trust stays manual.** Rejected: a rendezvous server
(not peer-to-peer) and automatic promotion of gossiped peers (turns one compromised peer into
mesh-wide compromise). SSDP adopted for the local segment but rejected as primary — its
multicast does not cross WireGuard, so it fails between my own machines, not just across the
internet.

**No consensus protocol.** Rejected: Raft or a quorum for the compaction lease. The operation
being protected is idempotent-in-effect — duplicate output is merged by existing dedupe — so
the worst case of a lease race is wasted tokens, not corruption. A consensus implementation
would be more code than the entire rest of this design.

## Implementation surface

New code is confined to §schema changes, the protocol endpoints, discovery, and the lease.
Everything else follows from the store already being a content-addressed markdown tree.

Existing files touched:

- `aiforge_core/runtime/work_notes/_render.py` — `origin` / `rev` / `updated_by` in frontmatter
- `aiforge_core/memory/okf/nodes.py`, `okf/store.py` — compound-key identity, link resolution
- `aiforge_core/api/api.py` — mount the sync routes
- `aiforge_core/memory/md_store/_ingest.py` — ingest path for synced files (reuse, not change,
  if the existing `write()` suffices)
