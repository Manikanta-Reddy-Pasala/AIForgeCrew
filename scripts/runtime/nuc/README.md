# NUC systemd user units

The AIForge stack runs on the NUC as `systemd --user` units; the Mac Studio
keeps only the LLM serving (LM Studio) and its launchd jobs.

## Units

| Unit | What |
|---|---|
| `aiforge-api.service` | FastAPI + ticket runner + memory sync, on :8799 |
| `aiforge-embed-sidecar.service` | bge-m3 embeddings, :8764 |
| `aiforge-rerank-sidecar.service` | reranker, :8765 |
| `aiforge-git-pull.timer` | pulls this repo |
| `aiforge-repo-pull.timer` | pulls the managed target repos |
| `aiforge-memory-decay.timer` | memory decay pass |
| `aiforge-pr-comments.timer` | PR comment ingest |
| `aiforge-lms-ensure.timer` | keeps the Studio's LM Studio model loaded |
| `aiforge-worktree-janitor.timer` | reaps stale agent worktrees |
| `aiforge-reindex-daily.timer` | daily code reindex |

## Deploy

```bash
cd ~/AIForgeCrew && git pull --ff-only && bash scripts/runtime/nuc/deploy.sh
```

Idempotent, and the source of truth for what actually gets enabled. It pulls
both repos (AIForgeCrew and AiForgeMemory), reinstalls the editable packages,
copies the `%h`-relative units into `~/.config/systemd/user`, restarts the
services, enables the timers and runs health checks — API :8799, embed :8764,
rerank :8765, Neo4j :7687, Postgres :5432. It exits non-zero if any check fails.

`aiforge-reindex-daily.timer` ships here but is not in the script's enable list;
enable it by hand if you want it.

## Manual install

```bash
mkdir -p ~/.config/systemd/user
cp scripts/runtime/nuc/*.service scripts/runtime/nuc/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now aiforge-api
# then enable the timers you want from the table above
sudo loginctl enable-linger "$(whoami)"   # units run without a login session
```

## Cross-host tunnels

Two ssh tunnels are the only glue between the boxes; there is no rsync, and code
arrives by git pull.

- **NUC → Mac Studio**: `lm-tunnel.service` (lives on the NUC only, not in this
  repo) exposes the Studio's LM Studio as `NUC:1235`.
- **Mac Studio → NUC**: the Studio's Postgres client reaches NUC Postgres through
  a tunnel looped back to `127.0.0.1:5433`. This is needed because macOS Sequoia
  launchd sandboxes non-loopback LAN `connect()` calls — an agent dialling
  `192.168.70.191:5432` directly gets `No route to host` even though the same
  call works from an interactive shell.

`AIFORGE_LMS_HOST` must name the Studio exactly as the NUC's `~/.ssh/config`
does. That file keys the Studio's identity off its IP and the `ms` /
`mac-studio` aliases with `IdentitiesOnly yes`; the `.lan` mDNS name matches no
`Host` block, so ssh offers the default key, the Studio rejects it, and
`lms-ensure` fails every tick with `Permission denied (publickey)`.
