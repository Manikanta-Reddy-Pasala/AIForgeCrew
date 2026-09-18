# aiforge — the AIForge terminal client (and installer)

A thin client for AIForge: it starts the sandbox (a Docker container) and
streams the agent's work into your terminal. The engine runs in the sandbox, not
in the CLI. It talks to the sandbox on `127.0.0.1:8799`, where the sandbox also
serves the web UI (`/ui/`).

## Install: one file

The `aiforge` binary (`aiforge.exe` on Windows) is the whole installer. It carries
the AIForge source the sandbox is built from:

```bash
./aiforge install         # Windows: .\aiforge.exe install
```

puts `aiforge` on your PATH, builds and starts the sandbox, and prints the web UI
address. **Docker is the only prerequisite** (if it is installed but stopped,
`aiforge` starts it). The first install builds the image and installs the
sandbox's dependencies inside it — several minutes; later starts take seconds.
Then `cd` into a project and run `aiforge`. `aiforge uninstall` removes it and
keeps your data in `~/.aiforge`.

Build the binary on the OS you run it on: `make aiforge` → `dist/cli/aiforge`
(see [installer/cli/README.md](../../installer/cli/README.md)).

## From source (development)

Every OS needs Docker running. From a checkout, `aiforge` builds the sandbox with
`./run.sh`, so run it inside the repo (or set `AIFORGE_REPO=/path/to/AIForgeCrew`).
Needs Python 3.11–3.13.

### Ubuntu

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
aiforge --version

# 3. Start the sandbox (the first run builds the image — several minutes)
aiforge box up

# 4. Chat in a project
aiforge mount add ~/work/my-project    # let the sandbox see it (you approve it here)
cd ~/work/my-project
aiforge
```

On **Ubuntu 22.04** (Python 3.10) use its Python 3.11 package instead of step
2's venv:

```bash
sudo apt install -y python3.11 python3.11-venv
python3.11 -m venv .venv-cli && . .venv-cli/bin/activate
pip install -e packages/aiforge_cli
```

### macOS

Needs Docker Desktop. The system `python3` is too old; `uv` fetches 3.12:

```bash
cd AIForgeCrew
uv venv .venv-cli --python 3.12 && source .venv-cli/bin/activate
uv pip install -e packages/aiforge_cli
aiforge box up
cd ~/path/to/project && aiforge
```

### Windows

Needs Docker Desktop and Python 3.12 (python.org or `winget install Python.Python.3.12`).
In PowerShell:

```powershell
cd AIForgeCrew
py -3.12 -m venv .venv-cli
.venv-cli\Scripts\activate
pip install -e packages\aiforge_cli
aiforge box up
cd C:\path\to\project; aiforge
```

## Off the corporate network

The sandbox installs packages from the internal Artifactory by default. Where
that host does not resolve, export the public registries **before**
`aiforge install` (binary) or the first `aiforge box up` (checkout):

```bash
export UV_DEFAULT_INDEX=https://pypi.org/simple
export AIFORGE_NPM_REGISTRY=https://registry.npmjs.org/
# only if Docker Hub / the Ubuntu archive are not reachable either:
export AIFORGE_BASE_REGISTRY=<registry prefix>  AIFORGE_APT_MIRROR=<ubuntu mirror>
```

Set the model on the web UI's home page at `http://127.0.0.1:8799/ui/` (or
export `AIFORGE_LM_BASE_URL`). A model server on this machine is
`http://127.0.0.1:<port>/v1` on native Linux docker, and
`http://host.docker.internal:<port>/v1` on Docker Desktop (rootless docker: the
machine's LAN address). The environment is read whenever the box is
(re)created, so keep these exports in your shell profile; an already-running
box picks them up on `aiforge box restart`.

## Everyday use

| You want | Run |
|---|---|
| Chat in this folder | `aiforge` (or `aiforge "fix the flaky retry test"` to send one message; `-q` prints only the answer) |
| Stop the run | `Esc`, or `/stop` |
| Help inside a chat | `/help` |
| Everything | `aiforge help` |
| Let the sandbox see a folder | `aiforge mount add ~/work` · list: `mount ls` |
| Sandbox state / logs / shell | `aiforge box status` · `box logs --tail 50` · `box shell` |
| Stop the sandbox | `aiforge box down` |
| A second chat in the same repo | `aiforge worktree add fix-retry "make the retry test deterministic"` |
| Past chats | `aiforge sessions` · `aiforge resume <id>` |
| Jira / Confluence / GitLab / email credentials | `aiforge integrations set jira base_url=https://jira.internal default_project=ONE` · check: `integrations test jira` |
| Shell completion | `aiforge completion bash >> ~/.bashrc` (also zsh, fish, powershell) |
| Help for one command | `aiforge help mount` |

Settings, highest wins: flags → environment (`AIFORGE_CLI_PORT`, `AIFORGE_REPO`,
`AIFORGE_SANDBOX_IMAGE`) → `~/.config/aiforge/cli.toml` → defaults.

## AIForge on another machine

The CLI has no remote mode — it drives the sandbox on the machine it runs on.
To use AIForge that runs elsewhere (the nuc, say), run the CLI there
(`ssh nuc`, then `aiforge` in a project folder), or open the project there with
VS Code Remote-SSH and use the extension ([packages/aiforge_vscode](../aiforge_vscode/README.md)).
