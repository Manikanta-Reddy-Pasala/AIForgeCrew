# Installing AIForge — one file

AIForge installs from ONE binary per OS: `aiforge` (`aiforge.exe` on Windows).
It is the CLI, and it carries the AIForge source the sandbox is built from, so

```bash
./aiforge install
```

gives you all three:

- **the CLI** — `aiforge` on your PATH (`~/.local/bin`, or
  `%LOCALAPPDATA%\Programs\AIForge` on Windows);
- **the sandbox** — an Ubuntu 24.04 container built from the carried source and
  started (first time: a few minutes);
- **the web UI** — served by the sandbox at `http://127.0.0.1:8799/ui/`.

The only thing the machine needs is **Docker with Compose v2** (Docker Desktop
on macOS and Windows; `docker.io` + `docker-compose-v2` on Ubuntu; Docker's own
repo elsewhere — see [INSTALL.md](../INSTALL.md#prerequisites)). If Docker is
installed but stopped, `aiforge` starts it.

After that: set the model on the web UI's home page, `cd` into a project and run
`aiforge`. Re-run `aiforge install` from a newer binary to update (a new binary
builds a new sandbox image). `aiforge uninstall` stops the sandbox and removes
the CLI; your chats, settings and memory in `~/.aiforge` stay. Networking, the
environment it passes to the box, and use off the corporate network:
[INSTALL.md](../INSTALL.md#the-aiforge-binary).

The VS Code extension is installed separately — see
[packages/aiforge_vscode](../packages/aiforge_vscode/README.md).

## Building the binary

```bash
make aiforge                    # = installer/cli/build-binary.sh → dist/cli/aiforge[.exe]
```

Build on the OS you ship to (PyInstaller cannot cross-compile); details, CI and
publishing in [cli/README.md](cli/README.md).

The earlier native packages (.deb / .dmg / .msi / portable), which ran AIForge
directly on the machine without a sandbox, are retired: the binary replaces them.
