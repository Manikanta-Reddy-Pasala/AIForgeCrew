# Installing AIForge

## How it runs

`./run.sh` starts an Ubuntu 24.04 sandbox. That is the only mode.

- **Command** — `./run.sh`
- **Needs on the machine** — Docker + Compose
- **What the agent can touch** — everything **inside** the box; of this machine only `~/.aiforge` plus folders you approve
- **Rights** — full: your uid + passwordless sudo, no workspace jail
- **Network** — outbound open (`--isolated` closes it)

## Prerequisites

**Docker (with Compose) and `git`** — all your OS must provide. Python, Node and
tmux live inside the box; the image installs them.

| | macOS | Debian/Ubuntu | Fedora/RHEL | Windows |
|---|---|---|---|---|
| Docker | [Docker Desktop](https://www.docker.com/products/docker-desktop/) or `brew install --cask docker` | `sudo apt install -y docker.io docker-compose-plugin` | `sudo dnf install -y docker docker-compose-plugin` | Docker Desktop (WSL 2 backend) |

On Linux: `sudo usermod -aG docker $USER`, then log in again.

## First run: what gets installed

Only what the project declares, at its lockfiles' versions, from the internal
Artifactory. Later runs install nothing unless a lock changed.

| What | Versions from | Fetched from | Into |
|---|---|---|---|
| `uv` | `uv.lock` | Artifactory PyPI remote (`pip install uv==…`, once) | `.venv` |
| python deps, incl. `node`/`npm` (`toolchain` extra) | `uv.lock`, exported as pins | Artifactory PyPI remote (`uv pip install --no-build`) | `.venv` |
| web UI deps | `web/package-lock.json` | Artifactory npm remote (`npm ci --ignore-scripts`) | `web/node_modules` → `web/dist` |
| CodeGraph indexer | `scripts/codegraph/package-lock.json` | Artifactory npm remote (`npm ci --ignore-scripts`) | `.venv/codegraph` |

- **Artifactory only, wheels only.** No public fallback — the estate reaches
  neither pypi.org nor registry.npmjs.org, and run.sh stops and names the host
  if the index does not resolve. `--no-build` means nothing from an index is
  ever built from source; only this checkout's own two packages are built. The
  locks supply **versions, not sources**: the Python lock installs as exact
  pins from the index, npm swaps the public host for the configured registry,
  and uv.lock is never rewritten.
- **Nothing from GitHub, nothing fetched-and-executed** — no installer piped
  into a shell, no Node tarball, no managed CPython, no CDN browser binary, no
  npm install scripts. CodeGraph runs from its per-platform package with
  `CODEGRAPH_NO_DOWNLOAD=1` (its npm shim otherwise runs a GitHub bundle) and
  `CODEGRAPH_TELEMETRY=0`.

| | Default (committed) | Override per box | Credentials |
|---|---|---|---|
| PyPI | pyproject `[[tool.uv.index]]` | `UV_DEFAULT_INDEX` | `~/.netrc` (`machine artifactory.internal login … password …`) |
| npm | `AIFORGE_NPM_REGISTRY` in `aiforge.env` | `npm_config_registry` | `~/.npmrc` (`//artifactory.internal/:_authToken=…`) |

Optional extras (`structured`, `crawl`, `chunking`, `embed-static`, `xlsx`):

```bash
AIFORGE_EXTRAS=structured,crawl,chunking ./run.sh   # richer tools
./run.sh --install-model2vec                        # semantic memory (adds embed-static)
uv tool install graphifyy                           # the graphify CLI — NEVER into .venv
```

## Docker (default)

```bash
./run.sh                  # build the box (first time: a few minutes), start it
./run.sh --logs           # follow it — the first start installs its dependencies
./run.sh --shell          # a shell inside the box
./run.sh --stop           # stop it (keeps what the agent installed, until a rebuild)
./run.sh --repos ~/code   # mount YOUR projects folder as the box's project root
./run.sh --mount /srv/x   # also mount /srv/x (repeatable)
./run.sh --isolated       # no route out except an allowlist proxy
./run.sh --port 9000      # any other flag is passed to the run.sh inside the box
```

- **The box** — Ubuntu 24.04 + OS packages (python 3.12, git, tmux, curl,
  rsync, sudo) + this checkout; no Python or npm packages at build time. On
  first start the entrypoint installs the internal CA, links your credentials,
  then runs this checkout's `run.sh` natively inside the box — the same
  lockfile-pinned, Artifactory-only, wheels-only install into `aiforge-state`.
- **What it sees** — `~/.aiforge` at the same path (settings, memory, tickets,
  chat workspaces, and `~/.aiforge/repos` where projects live by default), and
  nothing else of this machine unless you mount it.
- **What it may do** — anything inside the box, and it is told to **install
  every tool a task needs** (`sudo apt-get install maven`, `npm i -g …`) rather
  than stop. Box-local actions run without approval; actions reaching outside —
  `git push`, opening a PR, deleting data — still ask. Files it writes into
  `~/.aiforge` stay yours; installed tools survive `--stop`, a rebuild is fresh.
- **Network** — the host's stack; UI on `127.0.0.1:8799/ui/`.

### Mounting your own folders

`--repos DIR` mounts DIR at the same path and makes it the box's project root
(`AIFORGE_REPO_ROOT`); `--mount DIR` adds any other folder at its own path,
repeatable. Both are **host actions**: requested folders live in
`~/.aiforge/mounts.list`, which Settings and the chat's `mount_folder` tool
append to — but `~/.aiforge` is inside the box, so an entry there is a
*request, never a grant*. A folder is mounted only once this host approved it
(`--mount DIR`, or a `y` at the prompt on the next start); approvals live where
the box cannot reach them (`~/.config/aiforge/approved-mounts`). A
non-interactive run skips unapproved folders; `/`, your home folder and
non-folders are refused.

### Credentials

In `~/.aiforge/security/`, linked where every tool looks. Tokens entered in
Settings (GitLab, Jira, the model key) land here too.

| File | Used by |
|---|---|
| `security/netrc` | pip, uv |
| `security/npmrc` | npm |
| `security/gitconfig`, `security/ssh/` | git (identity, SSH keys) |
| `security/ca/custom-ca.pem` | the internal CA — installed into the box's trust store |

### Environment

Set in the shell that runs `./run.sh`; compose passes them through, and they
reach the box. `aiforge.env` holds what is identical on every box
and is never written to — the environment wins. Catalogue: `.env.example`.

| Var | Default | Purpose |
|---|---|---|
| `AIFORGE_LM_BASE_URL` | `aiforge.env` | the model endpoint |
| `AIFORGE_CONFIG_DIR` | `~/.aiforge` | state, settings, credentials (host side) |
| `AIFORGE_REPOS_DIR` | `~/.aiforge/repos` | project root (same as `--repos`) |
| `AIFORGE_WORKSPACE_DIR` | — | clamps the chat agent's file scope |
| `AIFORGE_APT_MIRROR` | archive.ubuntu.com | Ubuntu mirror (e.g. Artifactory's ubuntu remote) for the box's apt. Passed as the `APT_MIRROR` build arg **and** re-applied at every start, so the agent's own `apt-get` uses it too |
| `AIFORGE_BASE_REGISTRY` | Docker Hub | prefix for the `ubuntu:24.04` base image (`BASE_REGISTRY` build arg) |
| `AIFORGE_EGRESS_ALLOW_HOSTS` | — | CSV allowlist; the proxy's list under `--isolated` |
| `AIFORGE_PROXY_IMAGE`, `AIFORGE_UI_PROXY_IMAGE` | tinyproxy, nginx:alpine | `--isolated` proxy images |
| `AIFORGE_EXTRAS` | — | optional Python extras |
| `AIFORGE_CA_BUNDLE` | — | internal CA, PEM |
| `AIFORGE_CODEGRAPH_REPOS` | — | CSV repo paths to pre-index (else on first use) |
| `AIFORGE_ROLE`, `AIFORGE_ADMIN_URL`, `AIFORGE_SYNC_GROUP` | `aiforge.env` | memory sync hub/spoke |
| `UV_DEFAULT_INDEX`, `npm_config_registry` | Artifactory | registry overrides |
| `http_proxy`/`https_proxy`/`no_proxy` | — | passed through (upper and lower case) |

`docker volume rm <project>_aiforge-state` only forces a reinstall — your data
is in `~/.aiforge`, never in a volume.

## Isolated network (`--isolated`)

By default the box shares this machine's network stack, so the allowlist in
`aiforge_core/net/egress.py` governs only *declared* destinations (Jira,
GitLab, SMTP, MCP, telemetry) and page fetches — a `curl` in the agent's shell
never passes through it. `--isolated` moves the boundary into the network:

- **Replaces the compose base**, not an overlay: it selects
  `docker-compose.isolated.yml` *instead of* `docker-compose.yml`, because
  compose cannot take `network_mode: host` back off. **Cannot be combined with
  `COMPOSE_FILE`** — both are base files, and run.sh refuses rather than
  silently running the un-isolated one.
- **box** on an `internal` network with no route out; its
  `http_proxy`/`https_proxy` are fixed to the proxy in the compose file itself.
- **egress**: tinyproxy, the only container on both networks, **default-deny**,
  filtering to `AIFORGE_EGRESS_ALLOW_HOSTS`. run.sh regenerates
  `~/.aiforge/.sandbox/{tinyproxy.conf,allow.txt}` from that same variable on
  every start, so Settings and the network cannot disagree. Empty list, nothing
  reachable.
- **ui**: nginx publishes the UI on `${AIFORGE_HOST}:${AIFORGE_PORT}` — a
  container on an internal network cannot publish a port itself.

**The cost, stated plainly:** the model endpoint is no longer reachable at
`127.0.0.1` — that was the host's loopback, which the box used to share. Point
`AIFORGE_LM_BASE_URL` at `http://host.docker.internal:1234/v1` (the host stays
addressable by name) **and add that host to the allowlist**, or keep the model
on the `out` side.

## What the first run does

Inside the box, `run.sh` creates `.venv` (with `python -m venv` when `uv` is not
yet present), installs `uv` and the locked deps into it, builds the UI, installs
CodeGraph, starts the API. CodeGraph indexes each repo on first use; the
vendored RepoMap needs only its tree-sitter grammars, which arrive with the
python deps.

```bash
./run.sh --skip-web          # don't rebuild the UI
./run.sh --dev               # uvicorn --reload
./run.sh --test              # probe the configured model endpoint, then exit
```

### Air-gapped

After the first run nothing is fetched. For a box that never had a network,
pre-seed `.venv`, `web/node_modules` (or `web/dist`) and `.venv/codegraph` from
a box of the same OS/arch — or build the portable bundle with every locked
wheel baked in (`installer/BUILDING.md`, `build-portable.sh --offline`).

### Behind a corporate CA or proxy

Paste the root **and its intermediates** into Settings → *Local certificate
authority*, or set `AIFORGE_CA_BUNDLE` before the first run. `run.sh` publishes
it to `git`, `curl`, `npm`, `uv` and every subprocess before the first install,
**merged with the platform's own root bundle** — `SSL_CERT_FILE` *replaces* the
trust store rather than adding to it, so a corporate-root-only file fixes the
internal hosts and takes every public root with it (`invalid peer certificate:
UnknownIssuer`). A root alone is not enough when your server presents a bare
leaf: load the intermediates too, or you get `unable to get local issuer
certificate`. Verification is never disabled. `http_proxy`/`https_proxy` are
mirrored across upper/lower case with loopback kept direct.

## Security

The API runs shell commands and edits files over HTTP, and binds **loopback**
by default.

- **Off-loopback needs a token.** `./run.sh --host 0.0.0.0` (exported as
  `AIFORGE_BIND_HOST`) requires `AIFORGE_API_TOKEN=<secret>`; the app **raises
  on startup** otherwise, reading the **real listening sockets** rather than
  the env var, so `uvicorn --host 0.0.0.0` is refused too. One escape hatch:
  `AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1`, for an operator who fronts the API
  themselves (Cloudflare Access, a WireGuard-only proxy).
- **Behind a same-host reverse proxy, set `AIFORGE_TRUST_LOOPBACK=0`.** A
  loopback caller is otherwise trusted without a token, and with
  nginx/Cloudflared in front *every* request arrives from `127.0.0.1` and
  inherits that trust — an auth bypass. The flag makes the token mandatory for
  everyone (`/api/health` and the UI shell stay open).
- **`/admin` and `/api/admin/*` are loopback-only.** They follow the same
  loopback-or-token rule as the rest of the API, and on top of it refuse a
  remote caller with 403 *even with a valid token*, so a stolen token does not
  open the admin page from another machine.

The agent has full rights inside its box but sees only `~/.aiforge` and the
folders you approved. There is no host mode.
