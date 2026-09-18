# aiforge — the AIForge terminal client

A thin client for AIForge: it starts the sandbox (a Docker container) and
streams the agent's work into your terminal. The engine runs in the sandbox, not
in the CLI. It talks to the sandbox on `127.0.0.1:8799`.

**Every OS needs Docker running.** The first `box up` builds the sandbox image
from a checkout of this repo, so run it inside the repo (or set
`AIFORGE_REPO=/path/to/AIForgeCrew`).

Two ways to get the CLI:

- **From source** (`aiforge-cli`) — needs Python 3.11–3.13.
- **Standalone binary** (`aiforge`) — ~12 MB, needs only Docker; build it on the
  OS you run it on (see [installer/cli/README.md](../../installer/cli/README.md)).

## Ubuntu

Ubuntu 24.04 ships Python 3.12, which is all it needs:

```bash
# 1. Docker + Python (once)
sudo apt-get update
sudo apt-get install -y git python3 python3-venv docker.io docker-compose-v2
sudo usermod -aG docker "$USER" && newgrp docker      # docker without sudo

# 2. The CLI, from the repo
git clone https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew.git
cd AIForgeCrew
python3 -m venv .venv-cli
. .venv-cli/bin/activate
pip install -e packages/aiforge_cli
aiforge-cli --version

# 3. Start the sandbox (the first run builds the image — several minutes)
aiforge-cli box up

# 4. Chat in a project
aiforge-cli mount add ~/work/my-project    # let the sandbox see it (you approve it here)
cd ~/work/my-project
aiforge-cli
```

On **Ubuntu 22.04** (Python 3.10) get a newer Python with `uv` instead of step 2's
venv:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv .venv-cli --python 3.12 && . .venv-cli/bin/activate
uv pip install -e packages/aiforge_cli
```

**Standalone binary on Ubuntu** (build on the OLDEST Ubuntu you will run it on —
a binary built on 24.04 does not start on 20.04):

```bash
installer/cli/build-binary.sh --out ~/bin     # → ~/bin/aiforge
~/bin/aiforge box up
```

## macOS

Needs Docker Desktop. The system `python3` is too old; `uv` fetches 3.12:

```bash
cd AIForgeCrew
uv venv .venv-cli --python 3.12 && source .venv-cli/bin/activate
uv pip install -e packages/aiforge_cli
aiforge-cli box up
cd ~/path/to/project && aiforge-cli
```

## Windows

Needs Docker Desktop and Python 3.12 (python.org or `winget install Python.Python.3.12`).
In PowerShell:

```powershell
cd AIForgeCrew
py -3.12 -m venv .venv-cli
.venv-cli\Scripts\activate
pip install -e packages\aiforge_cli
aiforge-cli box up
cd C:\path\to\project; aiforge-cli
```

## Off the corporate network

The sandbox build installs packages from the internal Artifactory by default.
Where that host does not resolve, point it at the public registries in
`aiforge.env` (repo root) **before the first `box up`**:

```bash
UV_DEFAULT_INDEX=https://pypi.org/simple
AIFORGE_NPM_REGISTRY=https://registry.npmjs.org/
```

The model the agent uses is `AIFORGE_LM_BASE_URL` in the same file (or set it on
the web UI's home page at `http://127.0.0.1:8799/ui/`).

## Everyday use

| You want | Run |
|---|---|
| Chat in this folder | `aiforge-cli` (or `aiforge-cli "fix the flaky retry test"` to send one message; `-q` prints only the answer) |
| Stop the run | `Esc`, or `/stop` |
| Help inside a chat | `/help` |
| Everything | `aiforge-cli help` |
| Let the sandbox see a folder | `aiforge-cli mount add ~/work` · list: `mount ls` |
| Sandbox state / logs / shell | `aiforge-cli box status` · `box logs --tail 50` · `box shell` |
| Stop the sandbox | `aiforge-cli box down` |
| A second chat in the same repo | `aiforge-cli worktree add fix-retry "make the retry test deterministic"` |
| Past chats | `aiforge-cli sessions` · `aiforge-cli resume <id>` |
| Jira / Confluence / GitLab / email credentials | `aiforge-cli integrations set jira base_url=https://jira.internal default_project=ONE` · check: `integrations test jira` |
| Shell completion | `aiforge-cli completion bash >> ~/.bashrc` (also zsh, fish, powershell) |
| Help for one command | `aiforge-cli help mount` |

Settings, highest wins: flags → environment (`AIFORGE_CLI_PORT`, `AIFORGE_REPO`,
`AIFORGE_SANDBOX_IMAGE`) → `~/.config/aiforge/cli.toml` → defaults.

## AIForge on another machine

The CLI has no remote mode — it drives the sandbox on the machine it runs on.
To use AIForge that runs elsewhere (the nuc, say), run the CLI there
(`ssh nuc`, then `aiforge` in a project folder), or open the project there with
VS Code Remote-SSH and use the extension ([packages/aiforge_vscode](../aiforge_vscode/README.md)).
