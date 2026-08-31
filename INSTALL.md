# Installing AIForge

Two ways to run it. Both are **single-mode** (embedded SQLite + scoped-OKR
Markdown memory — no Postgres/Neo4j) and include the full toolchain: the
tree-sitter RepoMap, the model2vec semantic embedder (static embeddings + sqlite-vec, **no
torch**), and the structured / crawl / chunking extras.

| | Binary / native | Docker |
|---|---|---|
| Command | `./run.sh` (or `sudo ./run.sh`) | `./run.sh --docker` |
| Where deps live | installed into `.venv` on the host | baked into the image |
| Filesystem access | the whole host (it IS the host) | full host FS mounted at `/host` |
| Best for | a box you own / a VM | isolation, or a clean host |
| First run | fast (uv + npm) | slow (image build, ~2 GB) |

Both persist state so restarts/rebuilds keep your config, tickets, chat and
memory. Point either at your model on the home page: `http://localhost:8799/ui/`.

---

## Binary / native mode

Runs directly on the host — the agent has full filesystem + shell access (no
sandbox). `run.sh` bootstraps everything: it installs `uv` and a portable
Node if missing, creates the `.venv`, installs the deps, builds the UI, and
starts the API.

```bash
git clone <repo> && cd AIForgeCrew
./run.sh                    # runs as your user
```

**Run as root (sudo):** for full-filesystem access (operate on any path,
system dirs, other users' repos):

```bash
sudo ./run.sh
```

As root, `run.sh` skips the group-permission grants (root already has them) and
serves the API in the foreground. State lives under `~/.aiforge` of the invoking
user (root's `~/.aiforge` under `sudo`).

Optional:
```bash
./run.sh --install-model2vec    # one-time: semantic recall (model2vec, ~30 MB, no torch)
./run.sh --port 9000 --host 0.0.0.0   # bind elsewhere (needs AIFORGE_API_TOKEN off-loopback)
```

Only `git` + `curl` (or `wget`) need to pre-exist. `uv`, Node, python deps and
CodeGraph are installed automatically. (The RepoMap is vendored in-tree, so
there is nothing to install for it — only its tree-sitter grammars, which come
in with the python deps.)

---

## Docker mode

One self-contained container. **All dependencies are baked into the image** —
python deps, the **RepoMap** grammars, the **model2vec** semantic stack (static embeddings +
sqlite-vec, **no torch**, embed model pre-downloaded), **structured / crawl /
chunking**, plus the pre-built web UI. Nothing is fetched at run time.

```bash
./run.sh --docker
# equivalently:  docker compose up -d --build
```

The **entire host filesystem is mounted at `/host`** so the agent works on your
real repos and files, and its edits/worktrees land back on the host.

```bash
# Narrow what the container can see/touch (recommended):
AIFORGE_HOST_ROOT=$HOME ./run.sh --docker        # only your home
AIFORGE_HOST_ROOT=$HOME/code ./run.sh --docker   # only ~/code
```

Persistent state (config, SQLite, memory briefs/captures, HF model cache) lives
on the host under `./data/aiforge` (override with `AIFORGE_DATA_DIR`), so it
survives `--build` rebuilds.

Common env:

| Var | Default | Purpose |
|---|---|---|
| `AIFORGE_HOST_ROOT` | `/` | host path mounted at `/host` (the agent's workspace) |
| `AIFORGE_DATA_DIR` | `./data` | where persisted app state lives on the host |
| `AIFORGE_LM_BASE_URL` | `http://127.0.0.1:1234/v1` | your model endpoint (host loopback works — the container uses host networking) |
| `AIFORGE_EMBED_BACKEND` | `model2vec` | `hash` for keyword-only; `api` for an external `/v1/embeddings` |
| `AIFORGE_RUNNER_CONCURRENCY` | `0` | N>0 runs N ticket-runner loops in-container alongside the API |
| `PREFETCH_EMBED_MODEL` (build arg) | `1` | `0` = don't bake the embed model (smaller image; downloads on first use) |

Manage it:
```bash
docker compose logs -f aiforge      # tail
docker compose down                 # stop (state persists on the host)
docker compose up -d --build        # rebuild after a git pull
```

### Security

The API runs shell commands and edits files over HTTP. It binds **loopback**
by default. To expose it beyond localhost you must set **both**
`AIFORGE_BIND_HOST=0.0.0.0` **and** `AIFORGE_API_TOKEN=<secret>` — the app
refuses to boot on a non-loopback bind without a token. The refusal is decided
from the **real listening socket** (read off the running server), not from
`AIFORGE_BIND_HOST`, so `uvicorn --host 0.0.0.0` is refused too.

**Behind a reverse proxy on the same host, set `AIFORGE_TRUST_LOOPBACK=0`.**
With a token configured, a loopback caller is trusted *without* one by default
(reaching the socket locally already implies read/write access to the same
files). If nginx/Cloudflared/anything else on this box forwards to the API,
every request in the world arrives from `127.0.0.1` and would inherit that
trust — turning the proxy into an auth bypass. `AIFORGE_TRUST_LOOPBACK=0` makes
the token mandatory for everyone (`/api/health` stays open).

The operator page `/admin` and `/api/admin/*` **always** require the token when
one is configured, loopback or not. A browser cannot send a header on a plain
navigation, so with a token set reach it as
`curl -H "Authorization: Bearer $AIFORGE_API_TOKEN" http://127.0.0.1:8799/admin`
(or over an SSH tunnel with a header-setting client).

In docker mode the full host FS is reachable at `/host`; narrow it with
`AIFORGE_HOST_ROOT` unless you intend whole-host access.
