"""argv in, exit status out.

Thin on purpose: parse, build the config and palette, hand off to App. Every
command's behaviour lives next to the thing it operates on, and every command's
TEXT lives in commands.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, colors, completion, config
from . import client as api
from . import commands as tbl
from . import help as helptext
from .app import EXIT_ENV, EXIT_OK, EXIT_USAGE, App, Exit


def build_parser() -> argparse.ArgumentParser:
    # add_help=False: `aiforge help` is the documented surface and it renders
    # commands.py, so argparse's own -h must not print a second, thinner one.
    parser = argparse.ArgumentParser(prog="aiforge", add_help=False)
    parser.add_argument("args", nargs="*")
    parser.add_argument("-q", "--quiet", dest="quiet", action="store_true")
    parser.add_argument("-v", "--verbose", dest="verbose", action="store_true")
    parser.add_argument("--json", dest="json_events", action="store_true")
    parser.add_argument("--mode", dest="mode", choices=list(tbl.MODES), default="simple")
    parser.add_argument("--yes", dest="yes", action="store_true")
    parser.add_argument("--port", dest="port", type=int, default=None)
    parser.add_argument("--no-color", dest="no_color", action="store_true")
    parser.add_argument("--tail", dest="tail", type=int, default=200)
    parser.add_argument("-f", "--follow", dest="follow", action="store_true")
    parser.add_argument("--force", dest="force", action="store_true")
    parser.add_argument("--version", dest="version", action="store_true")
    parser.add_argument("-h", "--help", dest="help", action="store_true")
    return parser


def _verbosity(opts) -> int:
    if opts.verbose:
        return 1
    return -1 if opts.quiet else 0


def _help_topic(command: str | None, rest: list[str]) -> str | None:
    """What `-h` should describe: the argument, else the command itself.

    `aiforge mount -h` must document mount, not reprint the index — and only
    `help` puts its topic in `rest`.
    """
    if rest:
        return rest[0]
    return command if command != "help" else None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    try:
        opts = parser.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    opts.verbosity = _verbosity(opts)
    colors.enable_windows_ansi()
    pal = colors.Palette(False) if opts.no_color else colors.detect()
    cfg = config.load(opts)

    words: list[str] = list(opts.args)
    command = words[0] if words and words[0] in tbl.top_names() else None
    rest = words[1:] if command else words

    if opts.version or command == "version":
        print(f"aiforge {__version__}")
        return EXIT_OK
    if opts.help or command == "help":
        topic = _help_topic(command, rest)
        print(helptext.command_help(pal, topic) if topic
              else helptext.top_help(pal, version=__version__))
        return EXIT_OK
    if command == "completion":
        if not rest:
            print(helptext.command_help(pal, "completion"))
            return EXIT_USAGE
        print(completion.script(rest[0]), end="")
        return EXIT_OK

    app = App(cfg, pal, cwd=Path.cwd())
    app.mode = opts.mode
    app.force = bool(opts.force)
    try:
        return _dispatch(app, command, rest, opts)
    except Exit as exc:
        if exc.message:
            print(exc.message, file=sys.stderr)
        return exc.code
    except (api.ApiDown, api.Busy, api.Stalled) as exc:
        # The sandbox answered badly or stopped answering. One line and an
        # environment status, not a traceback.
        print(f"aiforge: the sandbox API failed — {exc}\n"
              f"  aiforge box status    then   aiforge box logs --tail 50",
              file=sys.stderr)
        return EXIT_ENV
    except KeyboardInterrupt:
        return 130
    finally:
        app.client.close()


def _dispatch(app: App, command: str | None, rest: list[str], opts) -> int:
    """One place that decides which commands need a live sandbox.

    `box` and `mount` are the two that must work when the box is down — they
    are how you fix it — so they run before any boot.
    """
    if command == "box":
        return app.box_command(rest or ["status"], tail=opts.tail, follow=opts.follow)
    if command == "mount":
        lines = app.mount_command(rest or ["ls"])
        if lines:
            print("\n".join(lines))
        return EXIT_OK

    if command == "integrations":
        app.ensure_api()
        lines = app.integrations_command(rest or ["ls"])
        if lines:
            print("\n".join(lines))
        return EXIT_OK

    # Usage errors are settled BEFORE the sandbox is touched: `aiforge attach`
    # with no id used to boot (creating a session) and then print usage.
    if command in ("attach", "resume") and not (rest and rest[0].isdigit()):
        print(helptext.command_help(app.pal, command))
        return EXIT_USAGE

    if command == "sessions":
        app.ensure_api()
        from .app import _session_lines
        print("\n".join(_session_lines(app._sessions_safe(), app.pal)))
        return EXIT_OK

    if command == "attach":
        # No boot: attaching must not create a session for this folder, and
        # must never offer to restart the box under the run being watched.
        app.ensure_api()
        return app.attach(int(rest[0]))

    app.boot()

    if command == "resume":
        app.session_id = int(rest[0])
        return app.interactive()

    message = " ".join(rest).strip()
    if message:
        return app.send(message)
    if not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        if piped:
            return app.send(piped)
        print("nothing to do: no message, and stdin is empty", file=sys.stderr)
        return EXIT_USAGE
    return app.interactive()
