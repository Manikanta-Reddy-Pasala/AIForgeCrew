# Installing AIForge

## Two ways to run it

| | **Docker (default)** | Native |
|---|---|---|
| Command | `./run.sh` | `./run.sh --native` |
| Needs on the machine | Docker + Compose | git + python 3.12 |
| What the agent can touch | everything **inside** an Ubuntu 24.04 box; of this machine only `~/.aiforge` | the whole machine |
| Rights | full: your uid + passwordless sudo, no workspace jail | your user |
| Network | outbound open | outbound open |

Docker mode is the controlled one: the agent can install anything and do
anything inside its box, and cannot read or write the rest of your machine.
See **Docker (default)** below.

## Prerequisites (native mode, and the box's own first start)

**`git` and `python 3.12`** — the two things only your OS can provide. Docker
mode needs only Docker; the box brings its own.

| | macOS | Debian/Ubuntu | Fedora/RHEL | Windows |
|---|---|---|---|---|
| python 3.12 | [python.org installer](https://www.python.org/downloads/macos/) | `sudo apt install -y python3.12 python3.12-venv` | `sudo dnf install -y python3.12` | `winget install Python.Python.3.12` |
| tmux *(optional)* | `brew install tmux` | `sudo apt install -y tmux` | `sudo dnf install -y tmux` | use WSL |

Then:

```bash
./run.sh
```

The first run installs what the project **declares, at its lockfiles'
versions, from the internal Artifactory** — and nothing else:

| What | Versions from | Fetched from | Into |
|---|---|---|---|
| `uv` | `uv.lock` | Artifactory PyPI remote (`pip install uv==…`, once) | `.venv` |
| python deps, incl. `node`/`npm` (`toolchain` extra) | `uv.lock`, exported as pins | Artifactory PyPI remote (`uv pip install --no-build`) | `.venv` |
| web UI deps | `web/package-lock.json` | Artifactory npm remote (`npm ci --ignore-scripts`) | `web/node_modules` → `web/dist` |
| CodeGraph indexer | `scripts/codegraph/package-lock.json` | Artifactory npm remote (`npm ci --ignore-scripts`) | `.venv/codegraph` |

Later runs install nothing unless a lock changed, so a set-up box boots with no
network. Without tmux the agent still boots; its shell tool just loses
`cd`/`export` persistence between calls.

Optional extras, same lockfile:

```bash
AIFORGE_EXTRAS=structured,crawl,chunking ./run.sh   # richer tools
./run.sh --install-model2vec                        # semantic memory (embed-static)
uv tool install graphifyy                           # the graphify CLI — NEVER into .venv
```

**Registries: the internal Artifactory only.** There is no public fallback —
the estate cannot reach pypi.org or registry.npmjs.org, so if the index host
does not resolve, run.sh stops and says which host to check.

| | Default (committed) | Override per box | Credentials |
|---|---|---|---|
| PyPI | pyproject `[[tool.uv.index]]` | `UV_DEFAULT_INDEX` | `~/.netrc` (`machine artifactory.internal login … password …`) |
| npm | `AIFORGE_NPM_REGISTRY` in `aiforge.env` | `npm_config_registry` | `~/.npmrc` (`//artifactory.internal/:_authToken=…`) |

uv.lock and the npm locks record public URLs, but only their **versions** are
used: the Python lock is exported as exact pins and installed from the index
(`uv sync` would download the URLs written in the lock), and npm swaps the
public host for the configured registry. uv.lock is never rewritten. Put the
internal CA in `AIFORGE_CA_BUNDLE` (or Settings) and pip, uv, npm and git all
trust it.

Nothing is downloaded from GitHub — every piece is a package from Artifactory —
and every Python dependency installs as a
**wheel** (`--no-build`): nothing from an index is ever built from a source
archive. Only this checkout's own two packages are built, locally. `run.sh` never fetches a source and
executes it — no installer piped into a shell, no Node tarball, no managed
CPython, no browser binary from a CDN, no npm install scripts. CodeGraph runs from its per-platform package with
`CODEGRAPH_NO_DOWNLOAD=1` (its npm shim otherwise downloads and runs a bundle
from GitHub when that package is missing) and `CODEGRAPH_TELEMETRY=0`.


---

## Native

Full filesystem + shell access, no sandbox. `run.sh` creates `.venv` (with
`python -m venv` when `uv` is not yet present), installs `uv` and the locked
deps into it, builds the UI, installs CodeGraph, starts the API.

```bash
git clone <repo> && cd AIForgeCrew
./run.sh                              # as your user
sudo ./run.sh                         # as root: any path, system dirs, other users' repos
```

Under `sudo`, group-permission grants are skipped (root has them) and state
lives in root's `~/.aiforge`.

```bash
./run.sh --install-model2vec          # one-time: semantic recall (~30 MB, no torch)
./run.sh --port 9000 --host 0.0.0.0   # off-loopback needs AIFORGE_API_TOKEN
./run.sh --skip-web                   # don't rebuild the UI
```

CodeGraph installs from its lockfile on first boot and indexes each repo on
first use (`AIFORGE_CODEGRAPH_REPOS=/a,/b` pre-indexes). The RepoMap is vendored in-tree —
only its tree-sitter grammars are installed, and they come in with the python
deps.

### Air-gapped

After the first run nothing is fetched. For a box that never had a network,
pre-seed `.venv`, `web/node_modules` (or `web/dist`) and `.venv/codegraph` from
a box of the same OS/arch — or use the portable `--offline` bundle
(`installer/BUILDING.md`).

### Behind a corporate CA or proxy

Paste the root **and its intermediates** into Settings → *Local certificate
authority*, or set `AIFORGE_CA_BUNDLE=/path/ca.pem` before the first run.
`run.sh` publishes it to `git`, `curl`, `npm`, `uv` and every subprocess before
the first install, so the installer trusts what the app trusts.

It publishes your CA **merged with the platform's own root bundle**, because
`SSL_CERT_FILE` *replaces* the trust store rather than adding to it. Publishing
a corporate-root-only file fixes the internal hosts and takes every public root
away with it — PyPI then fails with `invalid peer certificate: UnknownIssuer`
unless your proxy happens to re-sign it too.

A root alone is not enough when your server presents a bare leaf — load the
intermediates too, or you get `unable to get local issuer certificate`.
Verification is never disabled: a self-signed endpoint is pinned, not trusted
blindly. `http_proxy`/`https_proxy` are mirrored across upper/lower case with
loopback kept direct.

---

## Docker (default)

```bash
./run.sh                 # build the box (first time: a few minutes), start it
./run.sh --logs          # follow it — the first start installs its dependencies
./run.sh --shell         # a shell inside the box
./run.sh --stop          # stop it (the box keeps what the agent installed, until a rebuild)
./run.sh --repos ~/code  # mount YOUR projects folder instead of ~/.aiforge/repos
./run.sh --port 9000     # any other flag is passed to the run.sh inside the box
```

How it works:

1. **The box** — `Dockerfile` is Ubuntu 24.04 + OS packages (python 3.12, git,
   tmux, curl, sudo) + this checkout. It installs no Python or npm packages at
   build time.
2. **Its first start** — the entrypoint installs the internal CA, links your
   credentials, then runs this checkout's own `run.sh --native` inside the box:
   the same lockfile-pinned, Artifactory-only, wheels-only install as native
   mode, into the `aiforge-state` volume. Later starts install nothing unless a
   lockfile changed. The API, the ticket runner and the memory sync loop all
   run inside.
3. **What it sees** — `~/.aiforge` is mounted at the same path. That folder is
   everything AIForge keeps: settings, memory, tickets, chat workspaces, and
   `~/.aiforge/repos`, where projects live by default (clone or create them
   there, then point a chat or ticket at one). To work on a folder of your
   own instead, `./run.sh --repos /path/to/code` mounts it at the same path
   as the box's project root. Nothing else of this machine is visible to the
   agent.
4. **What it may do** — anything inside the box: it runs as your uid with
   passwordless sudo and is told to **install every tool a task needs**
   (`ensure_runtime`, `sudo apt-get install maven`, `npm i -g …`) rather than
   stop. Box-local actions (sudo, installs, chown, systemctl) run without
   approval; actions that reach outside the box — `git push`, opening a PR,
   deleting data — still ask. No workspace jail. Files it writes into
   `~/.aiforge` stay owned by you. Tools it installs survive `--stop`; an image
   rebuild (a code update) starts the box fresh.
5. **Network** — the host's network: the UI is on this machine's
   `127.0.0.1:8799`, outbound connections (Artifactory, GitLab, Jira, the
   model) just work.

Credentials go in `~/.aiforge/security/` — the box links them where every tool
looks:

| File | Used by |
|---|---|
| `security/netrc` | pip, uv (`machine artifactory.internal login … password …`) |
| `security/npmrc` | npm (`//artifactory.internal/:_authToken=…`) |
| `security/gitconfig`, `security/ssh/` | git (identity, SSH keys for GitLab) |
| `security/ca/custom-ca.pem` | the internal CA — installed into the box's trust store |

Tokens entered in Settings (GitLab, Jira, the model key) are stored there too.

| Var (in your shell) | Default | Purpose |
|---|---|---|
| `AIFORGE_LM_BASE_URL` | `aiforge.env` | the model endpoint |
| `UV_DEFAULT_INDEX`, `npm_config_registry` | Artifactory | registry overrides |
| `AIFORGE_APT_MIRROR` | archive.ubuntu.com | Ubuntu mirror (e.g. Artifactory's ubuntu remote) for the box's apt |
| `AIFORGE_BASE_REGISTRY` | Docker Hub | prefix for the `ubuntu:24.04` base image |
| `AIFORGE_EXTRAS` | — | optional Python extras, as in native mode |

`docker volume rm <project>_aiforge-state` only forces a reinstall — your data
is in `~/.aiforge`, never in a volume.


---

## Security

The API runs shell commands and edits files over HTTP. It binds **loopback** by
default. Exposing it needs **both** `AIFORGE_BIND_HOST=0.0.0.0` and
`AIFORGE_API_TOKEN=<secret>` — the app refuses to boot on a non-loopback bind
without a token. The check reads the **real listening socket**, not the env var,
so `uvicorn --host 0.0.0.0` is refused too.

**Behind a reverse proxy on the same host, set `AIFORGE_TRUST_LOOPBACK=0`.** A
loopback caller is otherwise trusted without a token — reaching the socket
locally already implies access to the same files. But with nginx/Cloudflared in
front, *every* request arrives from `127.0.0.1` and inherits that trust, turning
the proxy into an auth bypass. `AIFORGE_TRUST_LOOPBACK=0` makes the token
mandatory for everyone (`/api/health` stays open).

`/admin` and `/api/admin/*` **always** require the token when one is set. A
browser cannot send a header on a plain navigation, so reach it as:

```bash
curl -H "Authorization: Bearer $AIFORGE_API_TOKEN" http://127.0.0.1:8799/admin
```

In Docker mode (the default) the agent has full rights inside its box but sees
only `~/.aiforge` of this machine; in native mode (`--native`) it has your
user's full access to the whole machine.
