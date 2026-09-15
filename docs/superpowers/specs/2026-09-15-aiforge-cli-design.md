# AIForge CLI — design

Date: 2026-09-15
Status: approved, in implementation

A terminal front-end for AIForge: one command, a streaming chat, and a sandbox
it starts for you. The brain is the code that already ships — the FastAPI app,
`chat_pipeline`, `chat_agent` and the ADK pipeline, all inside the docker
sandbox. The CLI adds no agent logic.

## 1. Shape

Two artifacts, one brain.

```
packages/aiforge_cli/            own pyproject; deps: httpx + prompt_toolkit
  aiforge_cli/
    __main__.py    argv -> dispatch
    cli.py         subcommands (chat, box, mount, sessions, completion, help)
    commands.py    ONE table: name, args, help, completer — feeds help + completion
    config.py      ~/.aiforge/cli.toml + env
    colors.py      palette + truecolor/256/16/none detection
    paths.py       host path <-> box path, mount containment
    box.py         docker lifecycle: detect, up, pull progress, health poll
    client.py      REST + SSE over httpx
    sessions.py    boxpath -> session id map (~/.aiforge/cli-sessions.json)
    render.py      PURE: event dict -> styled lines
    tail.py        the one redrawn status line
    input.py       prompt_toolkit session, keys, completer
    app.py         the REPL: boot, read, send, stream, repeat
```

**Hard rule:** `aiforge_cli` imports nothing from `aiforge_core`. The engine's
dependency tree (ADK, scipy, tree-sitter, litellm) cannot be frozen into a
15 MB binary and is irrelevant on the host. A test asserts the import graph
stays clean.

### Binaries

PyInstaller one-file, one lane per OS, built by `installer/cli/build-binary.sh`
into the existing installer outputs:

| OS | artifact | ships in |
|---|---|---|
| macOS | `aiforge` (universal2, ad-hoc signed) | `AIForge-<ver>.dmg` |
| Linux | `aiforge` (x64 + arm64, glibc 2.31 baseline) | `aiforge_<ver>_<arch>.deb`, tarball |
| Windows | `aiforge.exe` (x64) | `AIForge-<ver>.msi` |

PyInstaller is a *build-time* tool: pinned in `installer/cli/pins.txt`, installed
into a throwaway venv from the configured index. It is NOT added to `uv.lock`,
so `run.sh`'s lockfile-only rule is untouched. If the index carries no
PyInstaller wheel, the fallback is the existing `installer/portable` bundle.

Host requirements: the binary and `docker`. No python, git or node.

## 2. Commands

```
aiforge                      interactive chat in the current folder
aiforge "fix the retry"      one-shot: stream, print, exit with status
aiforge box up|down|restart|status|logs|shell
aiforge mount add DIR | rm DIR | ls | approve DIR
aiforge integrations ls|get|set|test [jira|confluence|gitlab|email]
aiforge sessions | resume N | attach N
aiforge completion bash|zsh|fish|powershell
aiforge help [command] | version
```

`integrations` is settings only. USING Jira and Confluence needs nothing here:
they are agent tools inside the sandbox, so asking a chat to file an issue
already works and arrives as a rendered tool step. Secrets are write-only —
a read reports `configured` or `not set`, and `set` refuses a field the chosen
integration does not have (the server's models ignore unknown keys, so a typo
would otherwise answer 200 having saved nothing).

Local only. There is no remote/`--server` mode and no API token: the backend
trusts loopback, and `127.0.0.1:<port>` (default 8799, `~/.aiforge/cli.toml`)
is the only endpoint the CLI speaks to.

## 3. Boot — automatic, never asks

```
aiforge
 1 load config + env
 2 GET /api/health (1.5s)            200 -> step 5
 3 docker present? daemon up?        no -> one-line fix, exit 3
 4 docker compose -p aiforge up -d   (pull progress as lines)
   poll /api/health, 120s cap
 5 GET /api/runtime/mounts           cwd inside a mount?
   no -> the ONE prompt: mount it? [Y/n]   (Enter = yes)
         yes: append to ~/.aiforge/mounts.list AND to the host's
              ~/.config/aiforge/approved-mounts, then recreate the container
              REFUSED while any run is in flight (names the session; --force
              overrides and loses the run)
 6 session: sessions.json[boxpath] -> resume, else POST /api/chat/sessions {cwd}
 7 header, then the prompt
```

`compose` is used directly rather than `run.sh`, because `run.sh` needs bash +
python 3.12 and the binary must not.

`--yes` / `AIFORGE_CLI_AUTO_MOUNT=1` answers the mount prompt. Everything else
is silent: image pull, health wait, session create, model probe.

**Mounts are host files, not an API call.** `~/.aiforge/mounts.list` is
mounted into the box, so the agent can append to it — a line there is a
REQUEST. The host's answer lives in `~/.config/aiforge/approved-mounts`, which
the box cannot see, and only the intersection is ever mounted. That is the rule
`run.sh` already enforces, so both entry points agree; `GET /api/runtime/mounts`
is read for what a running box actually has. Folders that can never be
mounted: `$HOME` or above, `~/.config` (it holds the approvals file, so
mounting it would let the box approve its own future mounts), `~/.ssh`,
`~/.gnupg`, `~/.aws`, `~/.kube`, `~/.docker`, and `/etc`, `/run`, `/proc`,
`/sys`, `/boot`, `/dev`.

**Which container.** With an AIForgeCrew checkout on the host (identified by
this project's own markers, not merely by having a `run.sh`), `run.sh` owns the
box: it builds the image, generates the mount overlay and knows every env
passthrough. Without one — the normal case for a binary install, and the only
case on Windows, which has no bash — the CLI writes its own compose file from
an embedded template against a prebuilt image, with the port published on
loopback rather than host networking (Docker Desktop has no usable host
network). That file is written to `~/.config/aiforge/sandbox/`, mode 0600, NOT
inside the bind-mounted `~/.aiforge`: it lists every mounted folder and every
passthrough value, proxy credentials included.

### Windows paths

mac/linux mount a host folder at the same path, so the mapping is identity.
Windows maps `C:\Users\x\work` to `/host/c/Users/x/work`. Every path sent to the
API is translated out, every path shown to the user is translated back. Two
functions, table-driven, unit-tested.

## 4. Communication

HTTP/1.1 + SSE only. No new server routes.

```
POST /api/chat/sessions                    create (cwd = BOX path)
GET  /api/chat/sessions                    list (resume, completion)
POST /api/chat/sessions/{id}/message       send; response is the event stream
GET  /api/chat/sessions/{id}/attach        replay buffered events, then tail
POST /api/chat/sessions/{id}/stop          Esc
POST /api/chat/sessions/{id}/steer         typing while busy
POST /api/chat/sessions/{id}/approve       approval gate decision
POST /api/chat/kill-all                    Ctrl+C twice
POST /api/chat/sessions/{id}/compact       /compact
GET  /api/chat/models                      /model completion
GET  /api/runtime/mounts                   mount state
POST|DELETE /api/runtime/mounts            add/remove
GET  /api/health                           boot gate
```

Event vocabulary consumed from the stream (the producers' own names):
`attached, thought, tool_start, tool, delta, message, subtasks, subtask_update,
usage, approval, approval_expired, auto_approved, captured, plan_ready, ticket,
stopped, error, ping, done, builder_done, stage_start, stage_done`.
An unknown `type` renders as one dim line rather than an error — the backend
may add events the installed binary has not heard of.

**Reconnect:** a dropped stream re-issues `/attach`, which replays the run's
buffer. The renderer keys steps by `call_id` (or stream index) and suppresses
anything already printed, so a replay prints only the tail it missed. A second
terminal may `aiforge attach N` and watch the same run — the reason the design
talks to the server instead of running a TTY inside the box.

## 5. Rendering

Scrolling transcript, real scrollback. Finished lines are printed permanently;
only the bottom status line is redrawn (`\r` + clear to EOL, no alt screen).

| event | line |
|---|---|
| user turn | `▸ <text>` bright cyan marker |
| `thought` | `● thinking <text>` dim |
| `tool_start` | `○ <name> <args middle-ellipsized>` yellow, becomes the tail action |
| `tool` | that line rewritten `✓`/`✗` + duration + one-line digest |
| `delta` | the answer streaming inline |
| `message` | final answer: bold heads, cyan inline code, fenced blocks |
| `usage` | tail only: `ctx 18% (23k/128k) · req 7 · out 1.2k` |
| `approval` | tail paused, diff/command printed, magenta prompt `[a]llow [r]eject [A]llow-all` |
| `captured` | `⊕ rule captured: "…"` |
| `stopped` / `error` | `■ stopped by you` / `✗ error: …` + dim hint |
| `done` | `done 1m42s · 9 tools · 2 files · ctx 18%` |
| `ping` | nothing |
| team events | one collapsed line each (not v1's focus) |

Verbosity: `-q` answers only, default as above, `-v` full args and results,
`--json` raw events for piping. Not a TTY (pipe, CI) auto-degrades to the plain
log: no spinner, no `\r`, no color.

### Colour

Colour or no colour, decided once. Eight ANSI roles plus dim/bold and never a
background, so there is nothing for a 256-colour or truecolor ladder to add and
the user's theme stays in charge on a light terminal as well as a dark one. Off
for `NO_COLOR`, `TERM=dumb` and any non-TTY destination; `AIFORGE_CLI_COLOR=always`
forces it through a pipe. On Windows `TERM` is normally unset, which is not the
same as dumb, and VT processing is switched on at startup.

| role | colour |
|---|---|
| user marker | bright cyan |
| thought | dim |
| tool running | yellow |
| tool ok / boot ok | green |
| tool fail / error | red (bold for errors) |
| diff | green `+`, red `-`, dim `@@` |
| approval | magenta |
| answer code / links | cyan / blue underline |
| tail | dim, cyan spinner; `ctx` green -> yellow at 60% -> red at 85% |

## 6. In-chat commands, keys, completion

```
/mount [add|rm] DIR   /mounts   /cd DIR   /mode simple|plan|team
/model [name]   /new   /sessions   /resume N   /stop   /compact   /ctx
/box logs|shell|restart|status   /quick <ask>   /review-edits on|off
/help [cmd]   /exit
```

`Esc` stop · `Enter` send · `Alt+Enter` newline · `Ctrl+C` once clears input,
twice kill-all · `Ctrl+R` history search · `Up` history
(`~/.aiforge/cli-history`).

Completion, from `commands.py` in both directions:

`--force` overrides the in-flight-run guard on a mount change. A run in flight
is detected by asking each session's attach stream (its first event is the only
place the server reports `running`) — the session list carries no such field.

* **shell**: `aiforge completion <shell>` prints a static script generated from
  the table; installers place it. Frozen binaries cannot use runtime python
  completion hooks, so the script is plain shell.
* **in-chat** (`TAB`): `/` -> commands with descriptions; `/mode` -> the three
  modes; `/model` -> live `/api/chat/models` (60s cache); `/resume` -> live
  session list with title and age; `/mount add`, `/cd` -> directories;
  `@path` -> files, inserted for the agent to read.

An unknown `/xyz` is not an error: it passes through to the backend, which
already resolves `.aiforge/commands/*.md`.

`help` has three surfaces over the one table — `aiforge help`, `aiforge <cmd>
-h`, and `/help [cmd]` — so they cannot drift.

## 7. Failures

| case | behaviour |
|---|---|
| no docker | per-OS install hint, exit 3 |
| daemon down | per-OS start hint, exit 3 |
| box unhealthy | `aiforge box restart` hint; wedged run -> offer kill-all |
| 409 run in progress | switch to `/attach` instead of erroring |
| mount while run live | name the busy session, offer `--force` |
| model endpoint down | the `error` event verbatim + `box logs --tail 50` hint |
| no `ping` for 120s | reconnect via `/attach`, say so on screen |
| the stream keeps dropping | at most 5 reconnects, backing off 1/2/4/8/15s, then exit 3 |
| an approval with no terminal | rejected, never assumed — and an empty answer at the prompt is also a reject |

Exit codes: 0 ok · 1 agent error · 2 usage · 3 environment · 130 interrupted.

## 8. Scope

**v1**: simple mode, streaming, approvals, stop/steer, mounts, session
resume/attach, three OS binaries, completion, colour, `--json`.

**Later** (all already have routes, so additive): team/plan rendering beyond
one-liners, media upload, checkpoints, tickets.

## 9. Testing

* `render.py` as a pure function: event list in, text out; golden files, no tty.
* `paths.py` mapping tables (win/mac/linux) and mount containment.
* `sessions.py` map file: create, resume, corrupt file, missing dir.
* `commands.py` -> help and completion scripts are generated, not hand-written:
  a test asserts every command appears in all four shell scripts.
* Fake-server tests: a stub replaying recorded SSE transcripts, including a
  mid-stream drop plus `/attach` replay, asserting no duplicated lines.
* Import-graph test: no `aiforge_core` import from `aiforge_cli`.
* app-level tests over an injected `ask` callable and a fake client: boot asks
  exactly one question (the mount) and none once the folder is mounted,
  approvals default to reject, `attach N` adopts session N, a repeatedly
  dropping stream gives up instead of spinning, keys stop/steer/reset.
* `Tail` against a StringIO: no escape bytes when disabled, and a streamed
  answer that survives the spinner.
* e2e on the nuc, in docker, against a real box and model.

Everything runs on the nuc, in a clean container — never on the laptop.
