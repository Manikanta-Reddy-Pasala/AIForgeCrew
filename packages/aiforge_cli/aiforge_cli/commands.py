"""One table, every surface.

`aiforge help`, `aiforge <cmd> -h`, `/help` inside the chat, TAB completion and
the four generated shell-completion scripts all read THIS. A command added here
appears in all of them; a command added anywhere else appears in one and is a
bug waiting to be reported.
"""

from __future__ import annotations

from dataclasses import dataclass

# What a completer should offer for an argument. Resolved late (live lists come
# from the API), so the table stays data.
ARG_NONE = ""
ARG_DIR = "dir"
ARG_FILE = "file"
ARG_MODE = "mode"
ARG_MODEL = "model"
ARG_SESSION = "session"
ARG_SHELL = "shell"
ARG_TOGGLE = "toggle"
ARG_BOX = "box"
ARG_COMMAND = "command"
ARG_KIND = "kind"
ARG_WORKTREE = "worktree"

MODES = ("simple", "plan", "team")
SHELLS = ("bash", "zsh", "fish", "powershell")
BOX_ACTIONS = ("up", "down", "restart", "status", "logs", "shell")
MOUNT_ACTIONS = ("add", "rm", "ls", "approve")
INTEGRATIONS = ("jira", "confluence", "gitlab", "email")
INTEGRATION_ACTIONS = ("ls", "get", "set", "test")
WORKTREE_ACTIONS = ("add", "ls", "rm")


@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    help: str
    arg: str = ARG_NONE
    choices: tuple[str, ...] = ()
    long: str = ""
    examples: tuple[str, ...] = ()
    group: str = "chat"


# ── `aiforge <this>` ──────────────────────────────────────────────────────

TOP: tuple[Command, ...] = (
    Command("chat", "[message]", "start a chat here, or run one message and exit",
            group="main",
            long=("With no message you get an interactive session in the current folder.\n"
                  "With one, the turn streams and aiforge exits with the run's status —\n"
                  "which is what makes it usable from a script or a git hook."),
            examples=("aiforge", 'aiforge "why does the retry test flake?"',
                      'aiforge -q "list the sync entry points" > notes.txt')),
    Command("box", "<up|down|restart|status|logs|shell>", "the sandbox itself",
            arg=ARG_BOX, choices=BOX_ACTIONS, group="main",
            long=("The sandbox is started for you on first use, so these are for when\n"
                  "you want to look inside it or reset it."),
            examples=("aiforge box status", "aiforge box logs --tail 50", "aiforge box shell")),
    Command("mount", "<add|rm|ls|approve> [DIR]", "which host folders the sandbox can see",
            arg=ARG_DIR, choices=MOUNT_ACTIONS, group="main",
            long=("A folder is mounted only when it is BOTH listed in ~/.aiforge/mounts.list\n"
                  "and approved by this host. The agent can add to the list; only you can\n"
                  "approve, and `ls` shows which entries are still waiting."),
            examples=("aiforge mount ls", "aiforge mount add ~/work",
                      "aiforge mount rm ~/work")),
    Command("integrations", "<ls|get|set|test> [jira|confluence|gitlab|email]",
            "credentials for Jira, Confluence, GitLab and email",
            arg=ARG_KIND, choices=INTEGRATION_ACTIONS, group="main",
            long=("Using these is not a CLI feature: they are tools the agent has inside\n"
                  "the sandbox, so asking a chat to file a Jira already works. This is the\n"
                  "configuration side — the same thing Settings does in the web UI.\n"
                  "\n"
                  "Secrets are write-only. A read reports `configured` or `not set`, never\n"
                  "the value, and `set` with no token keeps the stored one."),
            examples=("aiforge integrations ls", "aiforge integrations get jira",
                      "aiforge integrations set jira base_url=https://jira.internal "
                      "default_project=ONE",
                      "aiforge integrations test confluence")),
    Command("worktree", "<add|ls|rm> [name]",
            "a second chat in the SAME repo, on its own branch",
            arg=ARG_WORKTREE, choices=WORKTREE_ACTIONS, group="main",
            long=("Two chats in two repos need nothing: a different folder is already a\n"
                  "different session. Two chats in ONE repo need a worktree, or the two\n"
                  "agents edit the same files.\n"
                  "\n"
                  "`add` creates <repo>/.worktrees/<name> on branch wt/<name> and starts a\n"
                  "chat there. It is inside the repo, so it is already inside the repo's\n"
                  "mount — no new mount, no box restart, nobody else interrupted.\n"
                  "\n"
                  "git runs inside the sandbox, so the host still needs only docker."),
            examples=("aiforge worktree add fix-retry",
                      'aiforge worktree add fix-retry "make the retry test deterministic"',
                      "aiforge worktree ls", "aiforge worktree rm fix-retry")),
    Command("sessions", "", "list chats, newest first", group="main",
            examples=("aiforge sessions",)),
    Command("resume", "<id>", "continue a chat by id", arg=ARG_SESSION, group="main",
            examples=("aiforge resume 12",)),
    Command("attach", "<id>", "watch a run already in flight", arg=ARG_SESSION, group="main",
            long=("Read-only follow of someone else's run — the web UI's, or another\n"
                  "terminal's. Ctrl+C detaches; it does not stop the run."),
            examples=("aiforge attach 12",)),
    Command("completion", "<bash|zsh|fish|powershell>", "print a shell completion script",
            arg=ARG_SHELL, choices=SHELLS, group="main",
            long=("Installers place these for you. Print one yourself if you use a shell\n"
                  "the package did not cover:\n"
                  "  aiforge completion zsh > ~/.zfunc/_aiforge"),
            examples=("aiforge completion bash", "aiforge completion zsh")),
    Command("help", "[command]", "this, or the long form for one command",
            arg=ARG_COMMAND, group="main",
            examples=("aiforge help", "aiforge help mount")),
    Command("install", "", "put aiforge on your PATH and set up the sandbox + web UI",
            group="main",
            long=("Run it once from the downloaded binary. It copies itself to\n"
                  "~/.local/bin (or %LOCALAPPDATA%\\Programs\\AIForge on Windows),\n"
                  "adds that to PATH, builds the sandbox from the source it carries\n"
                  "and starts it — the web UI is served at 127.0.0.1:8799/ui/.\n"
                  "Needs only docker. Re-run it to update."),
            examples=("./aiforge install", "aiforge.exe install")),
    Command("uninstall", "", "stop the sandbox and remove aiforge (keeps your data)",
            group="main", examples=("aiforge uninstall",)),
    Command("version", "", "print the version and exit", group="main"),
)

# ── `/this` inside a chat ─────────────────────────────────────────────────

SLASH: tuple[Command, ...] = (
    Command("/help", "[command]", "commands and keys", arg=ARG_COMMAND),
    Command("/mode", "<simple|plan|team>", "how the next turns run",
            arg=ARG_MODE, choices=MODES,
            long=("simple  one agent with tools — the default\n"
                  "plan    read-only: it explores and proposes, it does not edit\n"
                  "team    the full pipeline (planner, doer, verifier, learner)")),
    Command("/model", "[name]", "show or switch this chat's model", arg=ARG_MODEL),
    Command("/quick", "<ask>", "one turn with a hard step cap, for a small ask"),
    Command("/review-edits", "<on|off>", "hold file edits for your approval",
            arg=ARG_TOGGLE, choices=("on", "off")),
    Command("/new", "", "a fresh chat in this folder"),
    Command("/sessions", "", "list chats"),
    Command("/resume", "<id>", "switch to another chat", arg=ARG_SESSION),
    Command("/stop", "", "stop the run in flight (same as Esc)"),
    Command("/kill-all", "", "reset EVERY session on this machine (asks first)"),
    Command("/compact", "", "fold this chat's history into a summary now"),
    Command("/ctx", "", "context window, requests and mode"),
    Command("/mounts", "", "what the sandbox can see"),
    Command("/integrations", "[get|test] [kind]", "Jira / Confluence / GitLab / email",
            arg=ARG_KIND, choices=INTEGRATION_ACTIONS),
    Command("/mount", "<add|rm> DIR", "mount or unmount a folder", arg=ARG_DIR,
            choices=("add", "rm")),
    Command("/cd", "<dir>", "move this chat to another folder", arg=ARG_DIR),
    Command("/worktree", "<add|ls|rm> [name]", "a parallel chat in this repo",
            arg=ARG_WORKTREE, choices=WORKTREE_ACTIONS),
    Command("/box", "<logs|shell|restart|status>", "the sandbox", arg=ARG_BOX,
            choices=BOX_ACTIONS),
    Command("/exit", "", "leave (Ctrl+D also works)"),
)

KEYS: tuple[tuple[str, str], ...] = (
    ("Enter", "send"),
    ("Alt+Enter", "newline"),
    ("Esc", "stop the run in flight"),
    ("Tab", "complete a command, a path or @file"),
    ("Up / Ctrl+R", "history, history search"),
    ("Ctrl+C", "once: clear the line · twice: stop everything"),
    ("Ctrl+D", "exit"),
)

GLOBAL_FLAGS: tuple[tuple[str, str], ...] = (
    ("-q, --quiet", "answers only"),
    ("-v, --verbose", "full tool arguments and results"),
    ("--json", "raw event stream, one JSON object per line"),
    ("--mode MODE", "simple (default), plan or team"),
    ("--yes", "answer the mount prompt with yes"),
    ("--port N", "API port (default 8799)"),
    ("--no-color", "never colour the output"),
    ("--version", "print the version and exit"),
)


def by_name(name: str, table: tuple[Command, ...]) -> Command | None:
    return next((c for c in table if c.name == name), None)


def top_names() -> list[str]:
    return [c.name for c in TOP]


def slash_names() -> list[str]:
    return [c.name for c in SLASH]
