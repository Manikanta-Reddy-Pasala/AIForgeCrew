# aiforge — the terminal command

`aiforge` chats with the AIForge agent from your terminal. It starts the sandbox
(a Docker container) when needed, and the sandbox also serves the web app at
<http://127.0.0.1:8799/ui/>.

To install it, see **[INSTALL.md](../../INSTALL.md)**. In short: `./aiforge install`.
Check it with `aiforge --version`.

## Everyday use

| You want | Run |
|---|---|
| Chat in this folder | `aiforge` |
| Send one message | `aiforge "fix the flaky retry test"` (add `-q` to hide tool steps) |
| Stop the agent mid-run | `Esc`, or `/stop` |
| Help inside a chat | `/help` |
| Let the sandbox see a folder | `aiforge mount add ~/work` (list: `aiforge mount ls`) |
| Sandbox status / logs / a shell in it | `aiforge box status` · `aiforge box logs --tail 50` · `aiforge box shell` |
| Stop / restart the sandbox | `aiforge box down` · `aiforge box restart` |
| Past chats | `aiforge sessions` · `aiforge resume <id>` |
| A second chat in the same repo | `aiforge worktree add fix-retry "make the retry test deterministic"` |
| Jira / Confluence / GitLab / email login | `aiforge integrations set jira base_url=https://jira.internal token=<your token> default_project=ONE`, check with `aiforge integrations test jira` |
| Tab completion for zsh / fish / PowerShell | `aiforge completion zsh` (bash gets it at install) |
| All commands | `aiforge help` · `aiforge help <command>` |

Settings, highest wins: command-line flags → environment (`AIFORGE_CLI_PORT`,
`AIFORGE_REPO`, `AIFORGE_SANDBOX_IMAGE`) → `~/.aiforge/cli.toml` → defaults.

## AIForge on another machine

`aiforge` only drives the sandbox on the machine it runs on. To use AIForge on
another machine, run `aiforge` there (`ssh` in first), or open the project there
with VS Code Remote-SSH and use the [VS Code extension](../aiforge_vscode/README.md).

## Developing the CLI

From a checkout, with Docker running and Python 3.11+:

```bash
git clone https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew.git && cd AIForgeCrew
python3 -m venv .venv-cli && . .venv-cli/bin/activate
pip install -e packages/aiforge_cli
aiforge --version && aiforge box up                        # uses this checkout's ./run.sh
```

- **Windows:** `winget install Python.Python.3.12`, then `py -3.12 -m venv .venv-cli`
  and `.venv-cli\Scripts\activate`.
- **macOS:** the system `python3` is too old; install 3.12 (python.org or
  `brew install python@3.12`) and use `python3.12 -m venv`.
- **Ubuntu 22.04** has only Python 3.10: `sudo apt install python3.11 python3.11-venv`,
  then `python3.11 -m venv`.
- **Linux:** `newgrp docker` lets you use docker right after `usermod`, without
  logging out. To build the one-file binary: `make aiforge`
([installer/cli/README.md](../../installer/cli/README.md)).
