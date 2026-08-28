"""One cross-platform way to START AIForge — the thing every installer calls.

``run.sh`` is 1100 lines of bash that bootstraps a box AND runs the app. An
installer has already done the bootstrapping (that is what installing IS), so
what a packaged app needs is only the second half: the API, plus the two
background loops that make it more than a web server.

That half is written HERE, in Python, because the .deb, the .app and the .msi
all have to do exactly the same thing and only one of the three can run bash.
The supervision is deliberately the same shape run.sh gives it:

* **uvicorn** serving ``aiforge_core.api.api:app`` — the foreground process; when
  it exits, everything exits.
* **the ticket runner** (``aiforge_core.runtime.adk_runner``) — single-shot by
  design, re-run every ``AIFORGE_RUNNER_POLL_SEC`` (10s).
* **the memory sync loop** (``aiforge_core.memory.sync.loop``) — re-run every 30s.

Both loops are respawned rather than kept alive: each is written to do one pass
and exit, and a crash in either must never take the API down. They are started
as CHILD PROCESSES (not threads) so a hung pass can be killed, and they are torn
down on the way out — on Windows too, where there is no process group to signal.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8799

# Each loop's module and how long to wait before running it again.
_LOOPS = (
    ("aiforge_core.runtime.adk_runner", "AIFORGE_RUNNER_POLL_SEC", 10),
    ("aiforge_core.memory.sync.loop", "AIFORGE_SYNC_POLL_SEC", 30),
)

_IS_WINDOWS = os.name == "nt"

# The app speaks plain HTTP and binds loopback by default. That is not an
# oversight to be fixed with a scheme constant: there is no TLS here, and a
# self-signed certificate for 127.0.0.1 buys a browser warning and a truststore
# problem in exchange for nothing. Off-loopback is the case that matters, and it
# is guarded where it belongs — _announce() refuses to stay quiet about a
# non-loopback bind with no AIFORGE_API_TOKEN. Building the URL in ONE place
# also stops the two copies of it drifting apart.
_SCHEME = "http"


def ui_url(host: str, port: int) -> str:
    """The address to hand a human, with 0.0.0.0/:: shown as something they can
    actually click."""
    shown = "localhost" if host in ("0.0.0.0", "127.0.0.1", "::") else host
    return f"{_SCHEME}://{shown}:{port}/ui/"


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


class _Supervisor:
    """Respawns the background loops until asked to stop."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self._lock = threading.Lock()

    def _spawn(self, module: str) -> subprocess.Popen | None:
        # sys.executable, not "python": inside a .app bundle or an MSI install
        # there is frequently no python on PATH at all.
        kwargs: dict = {}
        if _IS_WINDOWS:
            # Own process group, so Ctrl-C in the console does not race us to
            # the children and leave half of them orphaned.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen([sys.executable, "-m", module], **kwargs)
        except OSError as exc:
            print(f"  ! could not start {module}: {exc}", file=sys.stderr)
            return None
        with self._lock:
            self._procs.append(proc)
        return proc

    def _run_loop(self, module: str, env_name: str, default_s: int) -> None:
        while not self._stop.is_set():
            proc = self._spawn(module)
            if proc is not None:
                while proc.poll() is None and not self._stop.is_set():
                    time.sleep(0.5)
                if self._stop.is_set():
                    _terminate(proc)
                    return
                with self._lock:
                    if proc in self._procs:
                        self._procs.remove(proc)
            # Interruptible sleep: a Ctrl-C during the gap must not wait it out.
            self._stop.wait(_env_int(env_name, default_s))

    def start(self, skip: set[str] | None = None) -> list:
        """Start every loop except ``skip``. Returns the loops it started, so a
        caller (and a test) can see what is actually supervised."""
        started = [t for t in _LOOPS if t[0] not in (skip or set())]
        for module, env_name, default_s in started:
            threading.Thread(target=self._run_loop,
                             args=(module, env_name, default_s),
                             name=f"aiforge-{module.rsplit('.', 1)[-1]}",
                             daemon=True).start()
        return started

    def stop(self) -> None:
        """Signal the loops and kill whatever is still running.

        Called from the signal handler AND from the normal exit path, so it has
        to be safe twice.
        """
        self._stop.set()
        with self._lock:
            procs, self._procs = list(self._procs), []
        for proc in procs:
            _terminate(proc)


def _terminate(proc: subprocess.Popen) -> None:
    """Ask, then insist. A background pass that ignores the ask (a wedged
    network read) must not keep the installer's app alive after the window is
    closed."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        with contextlib.suppress(OSError):
            proc.kill()


def _announce(host: str, port: int) -> None:
    print("")
    print(f"  AIForge → {ui_url(host, port)}")
    print("  storage: SQLite + scoped-OKR memory under "
          f"{os.environ.get('AIFORGE_CONFIG_DIR') or '~/.aiforge'}")
    if host not in ("127.0.0.1", "::1", "localhost") \
            and not os.environ.get("AIFORGE_API_TOKEN"):
        # Same rule run.sh enforces: off-loopback without a token is an open
        # box, and the installers make off-loopback a checkbox away.
        print("  ! bound off-loopback with no AIFORGE_API_TOKEN — anyone who can "
              "reach this port can drive the agent", file=sys.stderr)
    print("")


def _open_browser_when_up(host: str, port: int, timeout: float = 30.0) -> None:
    """Open the UI once the port answers — an installed app that opens a dead
    tab teaches the user it is broken."""
    import socket
    target = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            try:
                if s.connect_ex((target, port)) == 0:
                    webbrowser.open(ui_url(target, port))
                    return
            except OSError:
                pass
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="aiforge", description="Run the AIForge API and its background loops.")
    ap.add_argument("--host", default=os.environ.get("AIFORGE_HOST", DEFAULT_HOST))
    ap.add_argument("--port", type=int,
                    default=_env_int("AIFORGE_PORT", DEFAULT_PORT))
    ap.add_argument("--no-runner", action="store_true",
                    help="do not poll for tickets (API only)")
    ap.add_argument("--no-sync", action="store_true",
                    help="do not run the memory sync loop")
    ap.add_argument("--open", action="store_true",
                    help="open the UI in a browser once the port answers")
    args = ap.parse_args(argv)

    skip = set()
    if args.no_runner:
        skip.add("aiforge_core.runtime.adk_runner")
    if args.no_sync:
        skip.add("aiforge_core.memory.sync.loop")

    sup = _Supervisor()
    sup.start(skip)

    def _bye(_signum, _frame):
        sup.stop()
        raise SystemExit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        # not the main thread, or the platform has no such signal
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _bye)

    _announce(args.host, args.port)
    if args.open:
        threading.Thread(target=_open_browser_when_up,
                         args=(args.host, args.port), daemon=True).start()

    try:
        import uvicorn
        uvicorn.run("aiforge_core.api.api:app", host=args.host, port=args.port)
    except KeyboardInterrupt:
        pass
    finally:
        # The loops are children of THIS process; leaving them behind is how an
        # installed app ends up with three orphans polling after the window is
        # closed.
        sup.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
