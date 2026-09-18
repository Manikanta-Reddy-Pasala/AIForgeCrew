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
  and per file **Diff** (VS Code's diff editor, before ↔ after), **Explain**
  (what changed and why, in simple English — run read-only) and **Undo**.
- **Go back** on every message: puts the folder back the way it was before
  that message, using the checkpoint AIForge takes before each turn (files the
  agent created since are removed too).
- **Approvals** and **questions** show in the chat and as notifications, so a
  blocked run is noticed with the panel closed. `aiforge.reviewEdits` holds
  every file change for your approval, with its diff.
- The **Changed files** view lists everything the chat has changed.

## Setup

1. Install the `aiforge` CLI and run `aiforge box up` (or use
   **AIForge: Start the sandbox**).
2. Open a folder. On the first message the extension checks that the sandbox can
   see it; if not, **Mount this folder** runs `aiforge mount add <folder>` (you
   approve it on this machine) or you can chat in a scratch workspace.
3. A remote AIForge: set `aiforge.apiUrl` and run **AIForge: Set API token**
   (stored in VS Code's secret storage).

## Build

```bash
npm ci
npm run typecheck && npm test
npm run package        # dist/aiforge.vsix
```

The chat webview bundles the web UI's live-turn reducer
(`web/src/views/Chat.reduce.ts`), so a turn renders the same in both.
