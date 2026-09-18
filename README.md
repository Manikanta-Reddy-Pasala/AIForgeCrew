# AIForgeCrew

An AI coding assistant that runs on your own machine. Chat with it about your code,
or give it a ticket in plain words and it plans, writes, tests and opens the pull
request. It works with any OpenAI-compatible model: LM Studio, Ollama, vLLM,
OpenRouter, or a cloud key. No database server and no GPU needed.

It runs inside its own sandbox (a Docker container), so it can only touch the
folders you allow.

## Install

You need **Docker** ([how to install it](INSTALL.md#1-install-docker)). Then, in the
folder where you downloaded the `aiforge` file:

```bash
./aiforge install          # Windows (PowerShell): .\aiforge.exe install
```

This one command installs the `aiforge` command, builds the sandbox, and starts
the web app at **<http://127.0.0.1:8799/ui/>**. The first run takes a few minutes.

Step-by-step, including where to get the `aiforge` file: **[INSTALL.md](INSTALL.md)**.

## Use it

1. Open **<http://127.0.0.1:8799/ui/>**. Under **Models**, paste your model server's
   URL into **Base URL**, press **🔍 Identify models from URL** and click your model.
2. In a terminal, go to a project folder and run `aiforge`. It asks once whether
   the sandbox may see that folder, then you can chat.

```bash
cd ~/code/my-project
aiforge                      # chat here
aiforge "fix the failing test in utils.py"
aiforge box status           # is the sandbox running?
aiforge help                 # all commands
```

## Docs

| Doc | For |
|---|---|
| **[INSTALL.md](INSTALL.md)** | Installing, updating, uninstalling, fixing common problems |
| **[QUICKSTART.md](QUICKSTART.md)** | Setting up models, Jira/Confluence/GitLab, jobs, rules, skills |
| **[CLI](packages/aiforge_cli/README.md)** | Every `aiforge` terminal command |
| **[VS Code extension](packages/aiforge_vscode/README.md)** | Chat inside VS Code, with diffs and undo |
| **[docs/ADVANCED.md](docs/ADVANCED.md)** | Running from source, network isolation, certificates, configuration, internals |
| [docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md) · [docs/TOOLS.md](docs/TOOLS.md) · [docs/DECISIONS.md](docs/DECISIONS.md) | How it works, every tool, design decisions |
