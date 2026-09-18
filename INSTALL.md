# Installing AIForge

Four steps: install Docker, get the `aiforge` file, run `aiforge install`, set
your model.

## 1. Install Docker

| Your computer | Install |
|---|---|
| **macOS** | [Docker Desktop](https://www.docker.com/products/docker-desktop/), then open it once |
| **Windows** | [Docker Desktop](https://www.docker.com/products/docker-desktop/) (accept the WSL 2 option it offers), then open it once. Run `aiforge` from PowerShell |
| **Ubuntu** | `sudo apt install -y docker.io docker-compose-v2` then `sudo usermod -aG docker $USER`, and log out and back in |
| **Other Linux** | [Docker Engine](https://docs.docker.com/engine/install/) with the Compose plugin |

That is all your computer needs: Python, Node and everything else live inside the sandbox.

## 2. Get the `aiforge` file

Download it from the internal Artifactory (sign in with your company account). Open
`https://artifactory.internal/artifactory/generic-local/aiforge-cli/`, pick the newest
folder (named like `0.1.0-ab12cd3`), then your system's folder:

| Your computer | Folder | File |
|---|---|---|
| Linux (Intel/AMD) | `linux-amd64` | `aiforge` |
| Linux (ARM) | `linux-arm64` | `aiforge` |
| macOS | `macos-universal2` | `aiforge` |
| Windows | `windows-amd64` | `aiforge.exe` |

The Linux file needs glibc 2.39 or newer (Ubuntu 24.04, Debian 13, Fedora 40+).
On an older system, or if your system's file is missing, a developer can build
it from source: [installer/cli/README.md](installer/cli/README.md).

## 3. Install

In a terminal, in the folder where you downloaded the file:

```bash
chmod +x aiforge && ./aiforge install        # Linux
xattr -d com.apple.quarantine aiforge; chmod +x aiforge && ./aiforge install   # macOS
.\aiforge.exe install                        # Windows (PowerShell)
```

This puts `aiforge` on your PATH, builds the sandbox (a few minutes the first
time) and starts the web app. It worked when it ends with:

```text
  next: cd into a project and run aiforge
  web UI: http://127.0.0.1:8799/ui/   ·   uninstall: aiforge uninstall
```

Open a new terminal afterwards so the `aiforge` command is found. You can delete
the downloaded file now.

## 4. Set your model

Open **<http://127.0.0.1:8799/ui/>**. Under **Models**, paste your model server's
address into **Base URL** (and an **API key** if it needs one), press
**🔍 Identify models from URL** and click your model. AIForge decides which agent
uses it.

A model server running on the same computer (LM Studio uses port 1234, Ollama 11434):

- **Linux with Docker Engine:** `http://127.0.0.1:1234/v1`
- **Docker Desktop (macOS, Windows, Linux):** `http://host.docker.internal:1234/v1`,
  because the sandbox reaches your computer through that name

Then, in any project folder:

```bash
aiforge
```

## Update and uninstall

- **Update:** download the newer file and run its install the same way
  (`./aiforge install` in the download folder). The sandbox is rebuilt.
- **Uninstall:** `aiforge uninstall`. Your chats and settings in `~/.aiforge` are kept.

## Common problems

| Problem | Fix |
|---|---|
| `aiforge: command not found` | Open a new terminal (the PATH changed during install) |
| "docker is not installed / not running" | Install Docker (step 1) or start Docker Desktop. On Linux, did you log out and back in after `usermod`? |
| "The model didn't respond" | Add or fix the model on the web app's home page (step 4); **🔧 Test tools** on its row checks it |
| The first start is slow | Normal: it installs everything inside the sandbox once. Watch it with `aiforge box logs -f` |
| Anything else | `aiforge box status`, then `aiforge box logs --tail 50` |

**Outside the company network:** the sandbox downloads packages from the internal
Artifactory. On a network without it, set these once, open a new terminal, then
run the install:

```bash
# Linux / macOS: add to ~/.bashrc or ~/.zshrc
export UV_DEFAULT_INDEX=https://pypi.org/simple
export AIFORGE_NPM_REGISTRY=https://registry.npmjs.org/
```

```powershell
# Windows (PowerShell)
setx UV_DEFAULT_INDEX https://pypi.org/simple
setx AIFORGE_NPM_REGISTRY https://registry.npmjs.org/
```

Running from source with `./run.sh`, network isolation, company certificates,
proxies and all settings: **[docs/ADVANCED.md](docs/ADVANCED.md)**.
