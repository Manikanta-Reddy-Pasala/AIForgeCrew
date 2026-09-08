# Installing AIForge

## Prerequisites

**`git` and `python 3.12`.** run.sh installs nothing — it checks what is
missing and prints the command for your OS, then stops.

| | macOS | Debian/Ubuntu | Fedora/RHEL | Windows |
|---|---|---|---|---|
| python 3.12 | `brew install python@3.12` | `sudo apt install -y python3.12 python3.12-venv` | `sudo dnf install -y python3.12` | `winget install Python.Python.3.12` |
| uv | `brew install uv` | `sudo apt install -y pipx && pipx install uv` | `sudo dnf install -y uv` | `winget install astral-sh.uv` |
| tmux *(optional)* | `brew install tmux` | `sudo apt install -y tmux` | `sudo dnf install -y tmux` | use WSL |

Then, once:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[toolchain]'
(cd web && npm ci --ignore-scripts && npm run build)      # the UI
./run.sh
```

`node` and `npm` come from the `toolchain` extra (`nodejs-wheel-binaries`), so
that second command supplies them — you do not install Node separately unless
you prefer to. Without tmux the agent still boots; its shell tool just loses
`cd`/`export` persistence between calls.

Optional, same pattern:

```bash
uv pip install --python .venv/bin/python -e '.[structured,crawl,chunking]'  # richer tools
uv pip install --python .venv/bin/python -e '.[embed-static]'               # semantic memory
uv tool install graphifyy          # the graphify CLI — NEVER into .venv
bash scripts/install-codegraph.sh  # the CodeGraph indexer (npm, no sudo)
```

Everything comes from one of two places: a dependency this project declares, or
a command you ran. `run.sh` never fetches a source and executes it — no
installer piped into a shell, no Node tarball, no managed CPython, no browser
binary from a CDN.

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
`python -m venv` when `uv` is not yet present), installs `uv` and the deps into
it, builds the UI, starts the API.

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

CodeGraph installs from npm on first boot. The RepoMap is vendored in-tree —
only its tree-sitter grammars are installed, and they come in with the python
deps.

### Air-gapped

Nothing to do — that is the default. A blocked step names the missing piece and
its fix rather than coming up degraded. Pre-seed `.venv` and `web/node_modules`
(or `web/dist`) on such a box.

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
