# AIForge installers — macOS, Windows, Ubuntu

Three native packages, one runtime. Each package is a thin wrapper around the
same `aiforge` command (`aiforge_core.cli.serve`), so the three cannot drift
apart: the .app, the .msi and the .deb all start uvicorn plus the ticket runner
and the memory sync loop, exactly as `run.sh` does.

| | macOS | Windows | Ubuntu / Debian |
|---|---|---|---|
| Artifact | `AIForge-<ver>.dmg` | `AIForge-<ver>.msi` | `aiforge_<ver>_amd64.deb` |
| Installs to | `/Applications/AIForge.app` | `%ProgramFiles%\AIForge` | `/opt/aiforge` + `/usr/bin/aiforge` |
| Your runtime | `~/Library/Application Support/AIForge` | `%LOCALAPPDATA%\AIForge` | `~/.local/share/aiforge` |
| Your data | `~/.aiforge` | `%USERPROFILE%\.aiforge` | `~/.aiforge` |
| Start it | double-click AIForge | Start Menu → AIForge | `aiforge` |

Open <http://localhost:8799/ui/> — the mac and Windows launchers do it for you.

## What is (and is not) in the box

**In:** the app, the built web UI (inside the wheel — no npm on your machine),
and a `uv` binary.

**Not in:** a Python interpreter. Every target ships a different and usually
too-old one — Ubuntu 22.04 has 3.10, stock macOS has 3.9, Windows often has
none — so instead of bundling a 60 MB interpreter per package, `uv` provisions
the CPython 3.12 it needs on **first launch**. That first launch needs the
network; every launch after it does not.

Packages are ~130 MB (mostly `uv` plus the wheel). The runtime it builds in your
profile is a few hundred MB more.

## Per-user runtime, on purpose

The venv is built in **your** profile on first run, not by the installer as
root/admin. The agent runs as you and writes your repos, your git config and
your `~/.aiforge`; a runtime you cannot repair, extend or upgrade without an
administrator would be the wrong shape. The installed package stays immutable
and is replaced wholesale by an upgrade — which the first-run marker notices,
rebuilding the runtime without you having to know that is what happened.

## macOS

The .dmg is **not signed or notarised** (that needs an Apple Developer ID).
Gatekeeper will refuse the first open:

```
right-click AIForge.app → Open → Open      # once, then it is remembered
# or:
xattr -d com.apple.quarantine /Applications/AIForge.app
```

Double-clicking opens Terminal on purpose: this app is a server plus two
background loops, and its first run prints what it is downloading. A silent
Dock bounce is indistinguishable from a failure.

## Windows

Native — no WSL, no Docker Desktop. The MSI needs admin once (it writes Program
Files); everything after that is per-user.

**One real limitation.** The agent's `bash` tool keeps each run's shell state in
a tmux session, and there is no tmux on Windows. It degrades to a stateless
subprocess per command (`BashFallback`, `reason=tmux_missing`), so `cd` and
`export` do **not** carry between calls. Everything else — the API, chat, the
pipeline, memory, integrations — is unaffected. If you need that persistence,
install into WSL2 with the .deb instead.

## Ubuntu / Debian

```bash
sudo apt install ./aiforge_<ver>_amd64.deb
aiforge                 # first run downloads its Python, then starts
```

`python3` is deliberately **not** a dependency (uv handles it), but `tmux` is:
without it the agent silently loses shell state between calls, and a package is
where that should be settled rather than discovered.

Removing the package leaves `~/.local/share/aiforge` and `~/.aiforge` alone —
that is your memory, tickets and chat history, not package state.

## Building them

```bash
installer/build_payload.sh              # web UI → wheel, + uv for all three targets
installer/linux/build-deb.sh            # needs dpkg-deb   (Linux, or a container)
installer/macos/build-dmg.sh            # needs hdiutil    (macOS)
installer/windows/build-msi.sh          # needs wixl       (brew install msitools)
```

or `make installers` for all three (skipping any whose toolchain is absent).
Artifacts land in `dist/installer/`.

`build-msi.sh` produces a real MSI from macOS/Linux via `wixl`, so all three
come out of one CI job rather than needing a Windows runner. Its **structure**
is verified there; its **behaviour** is not — run it on Windows before shipping
it to anyone.

## Bind address

The default is loopback. Binding elsewhere without a token is an open box, and
the launcher says so:

```bash
aiforge --host 0.0.0.0 --port 8799      # set AIFORGE_API_TOKEN as well
```

## Options

```
aiforge [--host H] [--port N] [--no-runner] [--no-sync] [--open]
```

`--no-runner` serves the API without claiming tickets; `--no-sync` skips the
memory sync loop. Both loops otherwise restart on their own schedule
(`AIFORGE_RUNNER_POLL_SEC`, `AIFORGE_SYNC_POLL_SEC`) and are killed with the
app — closing the window does not leave them polling.
