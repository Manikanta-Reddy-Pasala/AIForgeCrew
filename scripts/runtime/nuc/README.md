# NUC systemd user units

Services that moved off Mac Studio launchd onto NUC systemd --user.

## Topology (post-migration, 2026-04-23)

```
Mac Studio 192.168.70.185          NUC 192.168.70.191 (static)
---------------------------------  ---------------------------------
com.aiforge.lmstudio      (LLM)    aiforge-api.service      (FastAPI:8799)
com.aiforge.embed-sidecar (LLM)    aiforge-git-pull.*       (timer 10m)
com.aiforge.graph-runner  (orch)   aiforge-repo-pull.*      (timer 5m, git pull)
com.aiforge.caffeinate             aiforge-memory-decay.*   (timer, decay)
com.aiforge.pg-tunnel     (bridge)
```

The pg-tunnel (MS→NUC postgres loopback) and reverse NUC→MS ssh tunnel
(`lm-tunnel.service` on NUC, exposes MS LM Studio as NUC:1235) are the
only cross-host glue. No rsync anywhere; code comes from GitHub.

## Deploy (preferred — scripted, idempotent)

```bash
cd ~/AIForgeCrew && git pull --ff-only && bash scripts/runtime/nuc/deploy.sh
```

Pulls both repos, reinstalls packages, syncs the (%h-relative) systemd
user units, restarts services, enables every timer, and runs health
checks. Exits non-zero on a failed health check.

## Install (manual, one-time reference)

```bash
# One-time on NUC
mkdir -p ~/.config/systemd/user
cp /path/to/AIForgeCrew/scripts/runtime/nuc/*.service ~/.config/systemd/user/
cp /path/to/AIForgeCrew/scripts/runtime/nuc/*.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now aiforge-api
for t in aiforge-git-pull.timer aiforge-repo-pull.timer \
         aiforge-memory-decay.timer; do
    systemctl --user enable --now "$t"
done
# Allow systemd user services to run without a logged-in session:
sudo loginctl enable-linger $(whoami)
```

## Reverse-direction tunnels

Mac Studio's postgres client (graph-runner) hits NUC Postgres via an ssh
tunnel that loops the remote port back to MS:127.0.0.1:5433. See
`../com.aiforge.pg-tunnel.plist`. This is needed because macOS Sequoia
launchd sandboxes non-loopback LAN `connect()` syscalls — agents that
dial `192.168.70.191:5432` directly get `No route to host` even though
the same call succeeds from an interactive shell.

NUC → Mac Studio LM Studio (port 1234) is exposed locally via
`~/.config/systemd/user/lm-tunnel.service` listening on NUC :1235.
