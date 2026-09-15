"""argv in, exit status out.

Thin on purpose: parse, build the config and palette, hand off to App. Every
command's behaviour lives next to the thing it operates on, and every command's
TEXT lives in commands.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, colors, commands as tbl, completion, config, help as helptext
from .app import EXIT_OK, EXIT_USAGE, App, Exit


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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    try:
        opts = parser.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    opts.verbosity = 1 if opts.verbose else (-1 if opts.quiet else 0)
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
        # `aiforge mount -h` must describe mount, not reprint the index: the
        # command word is in `command`, and only `help` puts it in `rest`.
        topic = rest[0] if rest else (command if command != "help" else None)
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
        if not app.client.healthy():
            app.boot()
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
        if not app.client.healthy():
            app.boot()
        from .app import _session_lines
        print("\n".join(_session_lines(app._sessions_safe(), app.pal)))
        return EXIT_OK

    app.boot()

    if command == "attach":
        return app.attach(int(rest[0]))
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
