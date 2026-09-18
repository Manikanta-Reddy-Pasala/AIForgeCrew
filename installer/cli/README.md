# Building the `aiforge` CLI binaries

The binary is the ONE AIForge installer: the CLI, plus the AIForge source the
sandbox is built from (packed at build time by `pack_source.py`, ~11 MB), so `./aiforge install`
gives the CLI on PATH, the sandbox and the web UI it serves. The engine itself
is NOT inside it — it is built into the sandbox image on the user's machine —
which is why the binary is ~22 MB instead of ~2 GB.
One binary per OS, and **each one must be built on that OS** — PyInstaller
freezes the interpreter it runs on and cannot cross-compile.

The host that runs the binary needs **docker with compose and nothing else**: no
python, no git, no node. The machine that BUILDS it needs python 3, git and a
checkout of this repo.

## One command

```bash
installer/cli/build-binary.sh                 # -> dist/cli/aiforge[.exe]
installer/cli/build-binary.sh --out /tmp/out  # somewhere else
```

It creates a throwaway venv, installs the pinned PyInstaller from
`installer/cli/pins.txt` plus this package, packs the sandbox source (the files
git knows about under the paths the Dockerfile copies — tracked, plus new
untracked files `.gitignore` does not exclude; never secrets; LF line endings;
the same bytes for the same source, so the same image tag), freezes
`packages/aiforge_cli/aiforge_cli/_entry.py`, writes the four shell-completion
scripts next to the binary, and smoke-tests the result (`--version`, `help`).
Nothing is left behind but the output directory.

Everything comes from the configured index: `UV_DEFAULT_INDEX`, else the
default `[[tool.uv.index]]` in `pyproject.toml` — never pip's public default.
On a box that cannot reach the internal Artifactory, name another one:

```bash
UV_DEFAULT_INDEX=https://pypi.org/simple installer/cli/build-binary.sh
```

## Per OS

| OS | Where | Result |
|---|---|---|
| Linux x86_64 | any Linux box (the nuc) | ~22 MB; `ldd` shows only glibc/libz/libdl/libpthread |
| Linux arm64 | an arm64 box, or x86_64 with binfmt (see CI below) | same |
| macOS | a Mac — needs python **3.11–3.13** | similar, `universal2` (x86_64 + arm64), ad-hoc signed |
| Windows | a Windows box, from **Git Bash** | `aiforge.exe` |

**Build on the oldest Linux you intend to support.** glibc is forward- but not
backward-compatible, so a binary built on 24.04 will not start on 20.04.

**macOS interpreter.** The script needs 3.11–3.13; PyInstaller 6.11 does not
support 3.14, and `/usr/bin/python3` (3.9) is too old for the package. If the
Mac's only `python3` is 3.14, point the script at a 3.12 one:

```bash
mkdir -p /tmp/pybin && ln -sf ~/.mlx/bin/python /tmp/pybin/python3   # e.g. 3.12.13
PATH=/tmp/pybin:$PATH installer/cli/build-binary.sh --out /tmp/cli-dist-mac
lipo -archs /tmp/cli-dist-mac/aiforge      # expect: x86_64 arm64
```

macOS refuses to run an unsigned arm64 binary, so the script ad-hoc signs it.
That is enough to run locally; for distribution outside the estate it needs a
Developer ID signature and notarisation.

**Windows** ships `python`, not `python3` — the script accepts either.

## In CI

`.gitlab-ci.yml` has a `package` stage:

* **`cli:linux-amd64`** — builds natively on the Linux runner. Runs on every
  pipeline; the binary and its completions are artifacts.
* **`cli:linux-arm64`** — the same build inside an `arm64` container. Needs
  either an arm64 runner or binfmt/qemu on an x86_64 one, so it is `manual` and
  `allow_failure`: a runner without binfmt should not fail the pipeline.
* **`cli:publish`** — `manual`. Uploads whatever the build jobs produced to
  Artifactory with `curl -k`.

There is no macOS or Windows job, because a Linux runner cannot produce those
binaries. Build them on a Mac and a Windows box with the commands above and
upload them with the same `curl -k` line the publish job prints.

## Working in parallel on one machine

This follows what the other terminal agents settled on, rather than inventing a
scheme:

* **opencode** runs a local server and attaches thin clients to it; a session
  lives on the server and clients come and go. AIForge is the same shape — the
  sandbox holds the sessions, `aiforge` is a client.
* **Claude Code** has no server and no container per session: every terminal is
  an independent process, state is keyed by project path, and the documented way
  to run several agents at once is **a git worktree per task**.
* **Codex CLI** and Claude Code both sandbox per command at the OS level rather
  than per session.

So, on one VM with several terminals:

| You want | Do this |
|---|---|
| Two chats in different repos | Just run `aiforge` in each. Different folder → different session → they run concurrently (only TEAM mode serialises). |
| Two chats in the SAME repo | `aiforge worktree add fix-retry` — the CLI makes the worktree, puts it on branch `wt/fix-retry`, and starts a chat in it. Add a message to send straight away: `aiforge worktree add fix-retry "make the retry test deterministic"`. |
| To see what is already parallel | `aiforge worktree ls` — the one you are in is marked. |
| To clean one up | `aiforge worktree rm fix-retry` — refuses while a chat is running in it (`--force` overrides). |
| To watch a run someone else started | `aiforge attach <id>` — read-only. Esc says so; Ctrl+C detaches and leaves the run alone. |
| To stop only your own run | `Esc`, or `/stop`. Scoped to your session. |
| To reset a wedged box | `/kill-all` — global, so it names the other running sessions and asks first. |

`worktree add` is deliberately cheap: the tree lands at
`<repo>/.worktrees/<name>`, which is **inside the repo's existing mount**, so
there is no mount change, no container restart, and nobody else on the machine
is interrupted. git itself runs **inside the sandbox** (which has git, and sees
the repo at the same path) as your own uid, so the host still needs nothing but
docker and the files it creates stay yours rather than root's.

If you run `aiforge` in a folder whose chat is already running in another
terminal, it offers the same thing rather than fighting for the session:

```
! chat #12 is already running in this folder (another terminal, or the web UI).
  [w] work in a new worktree  [a] attach read-only  [n] neither:
```

What one terminal **cannot** do to another:

* `box down` / `box restart` refuse while any run is in flight (`--force` says
  their work is lost).
* A second `Ctrl+C` will not fire the global reset when another session is
  running; it stops your chat and tells you to use `/kill-all` deliberately.
* Sending into a chat another terminal already runs answers 409, and the client
  falls back to **watching** it read-only instead of fighting for it.
* Two cold starts race for one box behind an flock: the first creates it, the
  rest wait.

The one thing still shared by everyone: **mounts**. One box means one mount set,
so a chat in repo A can read repo B's files if both are mounted. If that is not
acceptable, the isolation unit has to become a box per project (a container,
port and state volume each) — not built.

## Uploading by hand

```bash
# The same folder CI's cli:publish uses: CLI version + commit (the binary
# carries the whole app, so the CLI version alone is not unique).
VER=$(sed -n 's/^version = "\(.*\)"/\1/p' packages/aiforge_cli/pyproject.toml)-$(git rev-parse --short=8 HEAD)
curl -k -u "$ARTIFACTORY_USER:$ARTIFACTORY_TOKEN" \
     -T dist/cli/aiforge \
     "https://artifactory.internal/artifactory/generic-local/aiforge-cli/$VER/linux-amd64/aiforge"
```

`-k` skips certificate verification — fine against the internal host on a
trusted network, and the reason this is a manual step rather than something the
pipeline does silently.
