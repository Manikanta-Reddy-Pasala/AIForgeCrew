# AIForge for VS Code

Chat with the AIForge agent from the editor, the way Cursor's chat works:
streamed replies, the files each turn changed with their diffs, a plain-English
explanation of any change, and a way back to how things were before any message.

It is a thin client over the AIForge API — the same routes the web UI and the
`aiforge` CLI use. The agent runs in the AIForge sandbox; the CLI starts the box
and owns folder mounts, so the extension never grants the box access to anything.

## What it does

- **Chat** in the AIForge sidebar. Replies stream in; while a run is going,
  Send becomes **Steer** (your text is folded into the running task).
  **Agent** edits, **Ask** is read-only, **Team** runs the multi-agent pipeline.
- **Changed files** under every reply: status, +/− counts, the diff inline,
  and per file **Diff** (VS Code's diff editor: the file before that turn ↔ now),
  **Explain** (what that turn changed and why, in simple English — the turn's
  diff goes with the question; any edit would still ask you first) and **Undo**.
- **Go back** on every message: puts the folder back the way it was before
  that message, using the checkpoint AIForge takes before each turn (files the
  agent created since are removed too). As in Cursor, your next message then
  replaces that message and everything after it. A snapshot is taken first, so
  **Undo this** in the notification brings everything back.
- **Approvals** and **questions** show in the chat and as notifications, so a
  blocked run is noticed with the panel closed. `aiforge.reviewEdits` holds
  every file change for your approval, with its diff.
- The **Changed files** view lists everything the chat has changed.

## Build the extension (.vsix)

Needs Node.js 18+ and npm. From the repo root:

```bash
make vscode
# same as:
cd packages/aiforge_vscode
npm ci                 # installs the build tools from package-lock.json
npm run package        # → packages/aiforge_vscode/dist/aiforge.vsix
```

Behind the internal registry, point npm at it first
(`export npm_config_registry=$AIFORGE_NPM_REGISTRY`), as `run.sh` does.

Checks before shipping a build:

```bash
make vscode-test       # typecheck + unit tests (node:test)

# End-to-end, inside a real VS Code (downloaded once into .vscode-test/),
# against a RUNNING AIForge and a scratch git repo. On a server, use Xvfb:
AIFORGE_E2E_API=http://127.0.0.1:8799 AIFORGE_E2E_WS=/path/to/scratch-repo \
  xvfb-run -a npm run test:e2e
```

The end-to-end test asks the agent to fix a bug in `calc.py` in that repo
(`def add(a, b): return a - b`), approves the edit, opens the diff, asks for
the explanation and undoes the change — so use a throwaway repo.

## Install

**From the command line** (VS Code's `code` command on your PATH):

```bash
code --install-extension packages/aiforge_vscode/dist/aiforge.vsix
```

**From VS Code:** Extensions view (⇧⌘X / Ctrl+Shift+X) → `…` menu at the top →
**Install from VSIX…** → pick `aiforge.vsix`. Reload the window when asked.

To update, install the new `.vsix` the same way; to remove,
`code --uninstall-extension aiforge.aiforge-vscode`.

## First use

1. **Start AIForge.** Local: install the `aiforge` CLI and run `aiforge box up`
   (or click **Start it** when the extension says the sandbox is not running).
   The extension talks to `http://127.0.0.1:8799` by default.
2. **Open your project folder** and click the AIForge icon in the activity bar.
3. **Send a message.** If the sandbox can't see the folder yet, choose
   **Mount this folder** — it runs `aiforge mount add <folder>` in a terminal;
   you approve it on this machine and the sandbox restarts — or chat in a
   scratch workspace inside the sandbox.
4. **Remote AIForge** (another machine): set **AIForge: Api Url** in Settings
   and run **AIForge: Set API token** (the server's `AIFORGE_API_TOKEN`; kept in
   VS Code's secret storage). The agent then works on files on THAT machine, so
   for diffs and undo to show your files, open the folder there with VS Code
   **Remote-SSH** — the extension runs on the remote side (it is a workspace
   extension) and talks to AIForge on its loopback.

Settings: `aiforge.apiUrl`, `aiforge.cliPath` (the `aiforge` CLI),
`aiforge.mode` (agent / ask / team), `aiforge.reviewEdits` (approve every file
change, with its diff, before it lands).
