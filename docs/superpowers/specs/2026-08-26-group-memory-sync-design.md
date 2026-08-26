# Group-scoped memory sync, client-side filtering, and revertible admin state

Status: approved 2026-08-26. Extends
[`2026-08-18-admin-memory-sync-design.md`](2026-08-18-admin-memory-sync-design.md),
which introduced the hub-and-spoke replacement for the P2P mesh. That design
stands; nothing in it is superseded. This one adds four things on top:

1. **Groups.** One admin serves several independent fleets. A client discovers
   the group list from the admin url it already has, picks one, and everything
   it syncs is scoped to that group.
2. **A client-side filter.** The machine that authored a note decides whether it
   may leave — credentials never travel, and neither does the junk a user
   generates by asking the assistant what the capital of France is.
3. **Untouchable authored trees.** Sync may never write into `okf/` on either
   side. The client's notes and the admin's notes are the two things this
   feature must not be able to corrupt.
4. **Revert.** The admin can roll a group back to any recent state; so can a
   client roll back the fold it pulled.

There is still **no authentication** on the sync surface. That was a deliberate
choice for this deployment and it does not change here — see "Security posture".

---

## Why groups

Today a fleet is "every machine that names this admin". One admin can therefore
serve exactly one pool of knowledge. The operator runs more than one pool —
different customers, different sites — and wants one hub box rather than one per
pool.

An election, a registry or a per-group admin process would all work and all cost
more than the problem. A group is a **name the admin publishes and a client
selects**; everything else follows from scoping the memory tree by that name.

### Who owns the list

The admin. The client learns it.

The alternative — each client naming its own group — was rejected: a typo
silently creates a second group that looks like a working sync (the client
pushes happily, the admin accepts happily) and nobody notices until somebody
asks why two machines cannot see each other's knowledge.

* The list lives in the admin's **config dir**, `groups.json`:
  `{"groups": ["cellular", "retail"]}`. Config, not memory — it describes this
  deployment, never syncs, and survives a memory wipe.
* Seeded from `AIFORGE_SYNC_GROUPS` (comma-separated) on boot when the file does
  not exist, or created at runtime with `POST /api/admin/groups`.
* **No name is hardcoded anywhere.** `cellular` is the operator's example, not a
  default. An admin with no groups configured runs *ungrouped* — the exact
  behaviour of the 2026-08-18 design — so every existing install keeps working
  with no configuration and no migration.
* A group name is constrained to `paths.is_addressable`: it becomes a directory
  component, so the same alphabet that guards a peer id guards it. A name that
  does not round-trip is refused at creation, not repaired.

### How a client joins

`GET /api/memory/sync/groups` → `{"groups": [...], "admin": "<id>"}`.
Open, like the rest of the sync surface.

Resolution order on the client, highest first:

1. `AIFORGE_SYNC_GROUP` — the operator pins it and discovery is not consulted.
2. The cached selection in the config dir (`admin.json`, beside the learned
   admin id).
3. Discovery: the admin advertises **exactly one** group ⇒ select it and persist
   the choice. This is the single-group deployment, and it needs no UI at all.
4. Discovery: the admin advertises **several** and none is chosen ⇒ the cycle
   **stops before pushing anything** and records status
   `needs-group-selection`. The settings panel shows a picker. Nothing is sent
   to the wrong group, and nothing is logged on a loop.
5. The admin advertises none ⇒ ungrouped, legacy behaviour.

A selected group that later disappears from the admin's list is **kept, not
cleared**, and reported as `group-unknown`. Clearing it would silently re-run
auto-select and move a machine's knowledge into a different pool because
somebody was mid-edit on the admin.

The chosen group rides every sync call: `?group=` on `GET /manifest` and
`GET /blob/{digest}`, and a `group` field in the `POST /offer` and `POST /push`
bodies.

---

## Group isolation on disk

The rule: **a group scopes the whole memory tree on the admin.**

```
<memory>/                      admin's own authored knowledge — NEVER a sync target
  okf/                         (unchanged, untouched, see "Untouchable trees")
  captures/  compacted/  view/
  groups/
    cellular/
      peers/<origin>/<key>.md  what clients in this group pushed
      mesh/<admin-id>/<key>.md this group's tier-1 fold
      okf/                     empty on a pure hub; the admin may author here.
                               Authored, so also never a sync target.
      .snapshots/<stamp>/      revert points
    retail/
      ...
```

Implementation: `_io.root()` gains a **contextvar override**, and
`sync.group.scoped(name)` is a context manager that repoints the tree at
`<memory>/groups/<name>/` for the duration of a request or a fold. Everything
downstream — `paths`, `manifest`, `merge`, `apply`, `inbox`, `tiers` — is
already written against `_io.root()` and needs no change at all.

A contextvar, not `AIFORGE_MEMORY_MD_DIR`: the env var is process-global and the
API serves requests concurrently, so two clients in different groups would race
and one would write into the other's tree. The contextvar is per-task by
construction. `_io.root()`'s existing cache is keyed on the selecting env; the
override is consulted ahead of that cache.

Where the scope is entered:

* the four sync routes, on the group the caller named;
* `tiers.distil_mesh`, once per group, inside that group's scope;
* `/api/admin/*`, on the group being inspected.

An unknown group name on a sync route is **404 with the known list**, not a
silently-created directory. Auto-creation is how a typo becomes a second pool.

The client is unscoped. It belongs to exactly one group, so its tree is the
plain one it already has; the group name is something it *sends*, not something
it files by.

### The admin's own knowledge

Each group's fold reads that group's `peers/` and that group's `okf/`. The
admin's top-level `okf/` is **not** an input to any group fold.

This is the conservative direction: on a dedicated hub the admin authors
nothing, so nothing is lost, and it makes cross-group leakage structurally
impossible rather than a rule somebody has to remember. An operator who wants
the admin's own notes in a group can author them in that group's `okf/`.

---

## The filter: `aiforge_core/memory/sync/redact/`

A separate package with a narrow public API, running **on the client**, in the
push path. Not a separate process: a filter that can be down is a filter that
stops sync, and this one must be able to fail closed without taking the daemon
with it.

```python
review(node: dict) -> Verdict          # Verdict(send: bool, rule: str, reason: str)
explain() -> list[dict]                # the rules, for the settings screen
```

Three stages, in order, first refusal wins. All deterministic — no LLM in the
sync path, so the filter costs nothing, never rate-limits, and cannot wedge a
cycle when a model is down.

### `secrets.py` — credentials

Detects: known token shapes (AWS `AKIA…`, GitHub `ghp_`/`github_pat_`, Slack
`xox[abpr]-`, Google `AIza`, OpenAI/Anthropic key prefixes), JWTs, PEM
`-----BEGIN … PRIVATE KEY-----` blocks, `password=`/`passwd=`/`secret=`/
`api[_-]?key=`/`token=` assignments with a non-placeholder value, `Authorization:
Bearer …`, and URLs carrying inline credentials (`scheme://user:pass@host`).
Plus a high-entropy assignment heuristic: a `KEY = <40+ chars, Shannon entropy
above threshold, no spaces>` where the key name looks secret-ish.

**Verdict: block the whole node**, not scrub it. A note that mentions a
credential is a note *about* that credential — its title, its surrounding
sentence and its file path usually identify the system too. Scrubbing the
matched span ships the rest of that, and it ships it with an implicit claim of
safety. Blocking is also auditable: the operator sees "1 node held back, rule
`secrets.aws_key`" and can look at it.

The known-token patterns are the reliable half; the entropy heuristic is the
recall half and is the one to tune from the block log.

### `private.py` — personal scope

Blocks nodes whose scope is local/personal, and nodes whose only concrete
referent is a path under `$HOME` that is not inside a registered repo. A note
about somebody's own dotfiles is not fleet knowledge.

### `noise.py` — the country-name class

The user asks the assistant something idle, it becomes a capture, the capture
becomes a node, and the node syncs to everybody. Heuristics, each independently
tunable and each logged by name when it fires:

* **no project signal** — no file path, no code identifier, no repo/service
  name, no command, no error string;
* **bare single entity** — the whole node is one proper noun and a dictionary
  fact about it;
* **unanswered question** — a title in question form with a body that resolves
  nothing;
* **below substance threshold** — too few non-boilerplate characters once
  bullets, headings and frontmatter are removed.

Thresholds live in one constants block with the reasoning next to each, and are
overridable by env for tuning without a release.

### Where it runs

* **Client, in `push._mine`.** A blocked node is never advertised, so it never
  appears in an offer and the admin never learns it exists. This is the
  meaningful line of defence: the data does not leave the machine.
* **Admin, in `inbox.accept`.** Re-run as defence in depth, so an old client
  build that predates the filter cannot leak into a group. Cheap — the bytes are
  already in hand.

Blocked decisions are recorded to a bounded ring in the config dir
(`sync_filter.json`, last 200) with `{node, rule, reason, at}` so the settings
screen can show what was held back and why. The node itself is not copied into
that file — it may be the secret.

---

## Untouchable authored trees

`okf/` is the one directory whose sole writer is the machine it belongs to —
the client's own tree, the admin's top-level tree, and each group's `okf/`
inside its scope. The rule below is expressed against `_io.root()`, so entering
a group scope moves it onto that group's `okf/` automatically.
Corrupting it is the failure this feature must be structurally incapable of.

`paths.target_for` already prefers not to route there (`_is_ours`). That becomes
an enforced invariant:

* `_io.assert_not_ours(path)` raises on any write whose destination resolves
  inside `okf/`, excluding `okf/.tomb/` (tombstones are the one legitimate
  network-driven write there, and are already guarded to self-origin).
* Every network-driven writer calls it: `apply.apply_blob`, `inbox.accept`, and
  the class A path. A raise is a **refused record**, counted and logged — never
  a failed cycle.

The destination set for anything arriving over the network is therefore provably
`{peers/, mesh/, okf/.tomb/}`. This is belt-and-braces over `target_for`: a
future routing bug cannot reach authored notes, it can only refuse a record.

Writes stay staged-and-renamed as they already are, so a record is never
observed half-written.

---

## Smoother client-side merge

Two failure modes today leave the client worse than before the merge ran.

**`build_view` rebuilds in place.** A crash, an ENOSPC or a learner outage
part-way leaves `view/` — the working knowledge agents read — half old and half
new. It now builds into `view.tmp/` and swaps atomically at the end; a failed
build leaves the previous view exactly as it was and the next cycle retries.

**A pull applies node by node.** That part is correct and stays — per-record
application is what stops one bad record failing a cycle. What is added is the
snapshot below, so a *complete but wrong* fold is one call to undo.

---

## Revert

Cheap, because the tree is small markdown files and hardlinks make a snapshot
essentially free.

**Admin.** Before each group fold, `groups/<g>/` is snapshotted to
`groups/<g>/.snapshots/<utc-stamp>/` by hardlink copy. Last N kept
(`AIFORGE_SYNC_SNAPSHOTS`, default 10), oldest pruned.

* `GET  /api/admin/groups/{g}/snapshots` — list, newest first, with counts
* `POST /api/admin/groups/{g}/revert {"to": "<stamp>"}` — atomic swap back

A revert **snapshots the current state first**, so the revert is itself
revertible and an operator cannot destroy state with one wrong call.

**Client.** The same, on `mesh/` before a pull applies. A bad admin fold is one
call to undo locally without waiting for the admin to be fixed.

`.snapshots` is a dotted directory below the scanned roots, so
`_io._hidden_below` already excludes it from every manifest — a snapshot can
never be advertised, served or re-planted.

Both revert routes are loopback-only, on the existing `/admin` dependency.

---

## Status, and staying quiet

One file, `sync_status.json` in the config dir, written each cycle:

```json
{"admin": "http://nuc:8799", "group": "cellular",
 "groups_available": ["cellular", "retail"],
 "reachable": true, "state": "ok",
 "pending": 0, "blocked": {"noise.no_project_signal": 4},
 "pushed_total": 812, "last_ok": 1756200000, "last_error": null}
```

`state` is one of `ok`, `unreachable`, `needs-group-selection`,
`group-unknown`, `no-admin`.

**`pending` is computed, not queued.** It is the length of the offer's `want`
list — what the admin asked for and has not yet acknowledged. A successful push
makes the entry no longer wanted next cycle, so pending falls to zero by
construction. There is no outbox to leak, drift, or need clearing: the offer is
rebuilt from the tree every cycle. This is what "cleared once sent, and precise"
means here.

**Log quiet.** An admin that is down is normal operation, and today each failure
logs a line every cycle forever. Replaced by a state-change logger: the first
failure logs at WARNING, then nothing until the state changes or an hour passes,
at which point one line records that it is still down and for how long. The
error text lives in `sync_status.json` for the UI, not in the log. Recovery logs
one line. The existing `REPEATED_FAILURES` escalation stays — it distinguishes
"the admin is off" from "this machine is broken".

`GET /api/memory/sync/status` serves the file.

---

## Entry points

**`run.sh`**, mirroring the existing `--admin` / `--spoke` persistence:

* `--admin-url <url>` — persist `AIFORGE_ADMIN_URL` to the env file. Refused
  when this box holds the admin role, the same way `--admin` is refused when the
  url is set: a machine cannot be both.
* `--group <name>` — persist `AIFORGE_SYNC_GROUP`. Preselection for a headless
  box that will never see the settings screen.
* The boot banner gains one line: role, admin url, group, and reachability from
  the status file.

**Settings** (`web/src/views/Home.tsx`, a new "Memory sync" panel):

* admin url with a reachability badge (green / "unreachable, last seen 14:02" /
  "no admin configured");
* group: a picker when several are advertised, a plain label when one is, and a
  "select a group" prompt when the state is `needs-group-selection`;
* counters: pending, pushed total, blocked-by-rule with the reasons expandable;
* "Sync now", and a link to the snapshot/revert list.

---

## Security posture

Unchanged from 2026-08-18 and stated again because this design adds a
group-shaped thing that could be mistaken for one.

* **The sync surface takes no credential** (`AIFORGE_SYNC_AUTH=0`, the default).
  Bind the admin to a LAN or WireGuard address.
* **A group is not a security boundary.** It has no key. A client states its
  group and is believed, exactly as it states its peer id and is believed. The
  group check is a *routing and consistency* rule — it stops a misconfigured
  client writing into the wrong pool, not a hostile one on the same network.
  This was the explicit decision: no password for now, and the flow does not
  change if one is added later.
* **The filter is not access control either.** It stops this machine
  volunteering its secrets. It does not stop anything that can already reach the
  port.
* What the filter *does* add is real: the class of accident where a developer's
  capture containing a live token is folded into a node and replicated to every
  machine in the fleet.

---

## Testing

* `redact` — table-driven, one case per rule, both directions (a real AWS key
  blocks; `AKIA` in prose does not). The entropy heuristic gets its own
  false-positive set: base64 in a diff, a git SHA, a UUID.
* `group` — resolution order, single-group auto-select persists, multi-group
  halts without pushing, unknown-group 404 lists the alternatives, a name that
  is not addressable is refused.
* Isolation — two groups, two clients: what A pushes never appears in B's
  manifest, B's blob endpoint 404s on A's digest, and the two folds are
  independent. This is the test that would catch a contextvar leak.
* Untouchable trees — a crafted entry aimed at `okf/` is refused and counted;
  `okf/` mtimes are unchanged after a full cycle.
* Revert — snapshot, mutate, revert, verify; revert-the-revert; pruning keeps N;
  `.snapshots` never appears in a manifest.
* Merge smoothness — a `build_view` that raises part-way leaves the old `view/`
  intact and complete.
* Quiet — N failed cycles produce one warning, not N.
* Backwards compatibility — an admin with no groups and a client that sends no
  group behave exactly as they do on `main` today.

## Definition of done

Full suite green, SonarQube clean of new findings (cognitive complexity ≤ 15
per the org threshold — the scanner is ground truth, not a local meter), merged
to `main`.
