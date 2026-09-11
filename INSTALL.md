# Installing AIForge

## Prerequisites

**`git` and `python 3.12`** — the two things only your OS can provide.

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

## Two ways to run it

| | Native | Docker |
|---|---|---|
| Command | `./run.sh` (or `sudo ./run.sh`) | `./run.sh --docker` |
| Deps live | in `.venv` on the host | baked into the image |
| Filesystem | the whole host (it *is* the host) | host FS mounted at `/host` |
| Best for | a box you own / a VM | isolation, or a clean host |
| First run | fast | slow (~2 GB image build) |

Both are single-mode: embedded SQLite + Markdown memory, no Postgres/Neo4j. Both
include the tree-sitter RepoMap, the model2vec embedder (static embeddings +
sqlite-vec, no torch) and the structured / crawl / chunking extras. Both persist
state across restarts. Point either at your model on `http://localhost:8799/ui/`.

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

## Docker

One self-contained container, everything baked in — python deps, RepoMap
grammars, the model2vec stack (embed model pre-downloaded), the extras, and the
pre-built UI. Nothing is fetched at run time.

```bash
./run.sh --docker                     # same as: docker compose up -d --build
```

The **entire host filesystem is mounted at `/host`**, so the agent works on your
real repos and its edits land back on the host. Narrow it:

```bash
AIFORGE_HOST_ROOT=$HOME ./run.sh --docker
```

State (config, SQLite, memory, model cache) lives on the host under
`./data/aiforge`, so it survives rebuilds.

| Var | Default | Purpose |
|---|---|---|
| `AIFORGE_HOST_ROOT` | `/` | host path mounted at `/host` |
| `AIFORGE_DATA_DIR` | `./data` | where persisted state lives |
| `AIFORGE_LM_BASE_URL` | `http://127.0.0.1:1234/v1` | model endpoint (host networking, so loopback works) |
| `AIFORGE_EMBED_BACKEND` | `model2vec` | `hash` = keyword-only; `api` = external `/v1/embeddings` |
| `AIFORGE_RUNNER_CONCURRENCY` | `0` | N>0 runs N ticket-runner loops alongside the API |
| `PREFETCH_EMBED_MODEL` (build arg) | `1` | `0` = smaller image, model downloads on first use |

```bash
docker compose logs -f aiforge        # tail
docker compose down                   # stop (state persists)
docker compose up -d --build          # rebuild after a git pull
```

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

In Docker mode the whole host FS is reachable at `/host` — narrow it with
`AIFORGE_HOST_ROOT` unless you intend whole-host access.
