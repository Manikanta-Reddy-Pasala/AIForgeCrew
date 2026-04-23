# graph_rag auto-update

Indexes refresh automatically. Code changes land in Neo4j within 5-10 min
of `git push`. K8s state every 15 min. Memory every 5 min.

## What runs where

### NUC `192.168.70.191` (via systemd --user)

| Timer | Period | What |
|---|---|---|
| `aiforge-repo-pull.timer` | 5 min | `git pull --ff-only` on every `~/codeRepo/*` |
| `aiforge-git-pull.timer` | 10 min | same, legacy v3 (redundant) |
| `aiforge-memory-pull.timer` | 5 min | `git pull --ff-only` on `/srv/memory-repo` |
| `aiforge-file-indexer.timer` | 30 min | legacy v3 hash-delta indexer |
| `aiforge-reindex-daily.timer` | 02:00 | full wing rebuild |

### NUC — git hooks (fires per-pull)

43 repos under `~/codeRepo/*/.git/hooks/post-merge` → `graph_incremental.sh`

On each `git pull` that brings new commits:
1. Diff `HEAD@{1}..HEAD` for changed `.java / .ts / .py / .md` files
2. Extract only those files via javaparser / tsparser / pyparser
3. Delete existing `(:Method|:Class|:Function)` nodes scoped to the file
4. Ingest new JSONL into Neo4j (MERGE)
5. Re-embed only new / changed nodes via LM Studio nomic

### Laptop (via launchd)

| Label | Period | What |
|---|---|---|
| `com.aiforge.k8s-sync` | 15 min | `k8s_sync.py` QA cluster → rsync to NUC → `ingest_k8s.py` |

### Manual / not-yet-wired

| Action | When to run |
|---|---|
| `bin/memory_sync.sh` | Laptop → GitHub memory-repo. Needs `~/.claude/memory-repo` initialized with remote. Auto-fires via `com.aiforge.memory-push` launchd once set up. |
| `bin/graph_full_reindex.sh` | Only for cold start or schema change. Phase-0 nukes graph. |
| Prod k8s | Cert expired (see memory `incidents-prod-cert`). Rotate → same launchd as QA. |

## Flow diagram

```
laptop ~/.claude/memory/                ~/.kube/*-config
    │                                         │
    │ rsync (manual) OR post-merge            │ launchd 15 min
    ▼                                         ▼
GitHub memory-repo                    k8s_sync.py → JSONL → rsync → NUC
    │                                                          │
    │ git pull 5 min                                           ▼
    ▼                                                ingest_k8s → Neo4j
NUC /srv/memory-repo                                          ▲
    │ post-merge hook                                         │
    ▼                                                         │
ingest_memory + link_memories → Neo4j  ◀─── link_services ◀──┘
                                           (service-map.yaml)

GitHub code repos
    │ git pull 5 min
    ▼
NUC ~/codeRepo/*
    │ post-merge hook
    ▼
graph_incremental.sh (file-scoped delete+insert) → Neo4j → embed via LM Studio
```

## Verify

```bash
# NUC timers active?
ssh mani@192.168.70.191 'systemctl --user list-timers | grep aiforge'

# Last pull logs
ssh mani@192.168.70.191 'journalctl --user -u aiforge-repo-pull -n 20'

# Incremental re-index fired recently?
ssh mani@192.168.70.191 'tail -20 /tmp/graph_rag/incremental.log 2>/dev/null'

# Laptop k8s-sync?
launchctl list | grep aiforge
tail /tmp/aiforge-k8s-sync.log
```

## Stop / start

```bash
# NUC
ssh mani@192.168.70.191 'systemctl --user stop aiforge-repo-pull.timer aiforge-memory-pull.timer'

# Laptop
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.aiforge.k8s-sync.plist
```
