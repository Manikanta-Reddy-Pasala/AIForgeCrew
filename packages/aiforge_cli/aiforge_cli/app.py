"""Boot the sandbox, then talk to it until the user leaves.

The order here is the whole user-visible contract: nothing is asked except a
new mount, and everything else — image, container, health, session — is made to
exist quietly. Rendering decisions live in render.py, terminal control in
tail.py; this module is the sequence.

Every question the CLI can ask goes through the injected ``ask`` callable, so
"asks nothing but a mount" is a property a test can assert rather than a claim
in a docstring.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import box
from . import client as api
from . import commands as tbl
from . import help as helptext
from . import integrations as integ
from . import mounts as mountlist
from . import paths, sessions
from .colors import Palette
from .config import Config, approvals_file
from .keys import CTRL_C, ENTER, ESC, KeyWatcher
from .render import Renderer
from .tail import Tail

EXIT_OK = 0
EXIT_AGENT = 1
EXIT_USAGE = 2
EXIT_ENV = 3
EXIT_INTERRUPT = 130

# A dropped stream is re-attached, but not forever: a sandbox that has gone
# away would otherwise spin here silently for as long as the terminal is open.
MAX_RECONNECTS = 5
RECONNECT_BACKOFF = (1.0, 2.0, 4.0, 8.0, 15.0)


class Exit(Exception):
    """Leave with this status. The message, if any, is already printed."""

    def __init__(self, code: int, message: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message


def _attach_to(client, session_id: int) -> Callable[[], object]:
    """A factory that re-opens the run's stream, for the reconnect path."""
    return lambda: client.attach(session_id)


def _terminal_ask(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return ""


class App:
    def __init__(self, cfg: Config, pal: Palette, *, cwd: Path | None = None,
                 out=None, env: dict[str, str] | None = None,
                 client: api.Client | None = None,
                 ask: Callable[[str], str] | None = None,
                 interactive_stdin: bool | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.pal = pal
        self.cwd = Path.cwd() if cwd is None else cwd
        self.out = out or sys.stdout
        self.env = env
        self.client = client or api.Client(cfg.base_url)
        self.tail = Tail(self.out, pal=pal)
        self.render = Renderer(pal, verbosity=cfg.verbosity)
        self.session_id: int | None = None
        self.mode = "simple"
        self.role: str | None = None
        self.review_edits = False
        self.force = False
        self._sleep = sleep
        self._approve_all = False
        self._asked: list[str] = []        # every question, for the tests
        self._ask = ask or _terminal_ask
        if interactive_stdin is None:
            try:
                interactive_stdin = bool(sys.stdin.isatty())
            except Exception:  # noqa: BLE001
                interactive_stdin = False
        self.interactive_stdin = interactive_stdin

    # ── output helpers ─────────────────────────────────────────────────────

    def say(self, *lines: str) -> None:
        self.tail.write(list(lines))

    def ok(self, text: str, note: str = "") -> None:
        suffix = f"   {self.pal(note, 'dim')}" if note else ""
        self.say(f"{self.pal('✓', 'ok')} {text}{suffix}")

    def warn(self, text: str) -> None:
        self.say(f"{self.pal('!', 'warn')} {text}")

    def ask(self, prompt: str) -> str:
        """The only way this CLI asks the user anything."""
        self._asked.append(prompt)
        self.tail.clear()
        return self._ask(prompt).strip()

    # ── boot ───────────────────────────────────────────────────────────────

    def boot(self) -> None:
        if not self.client.healthy():
            self._start_box()
        mounted = self._ensure_mounted()
        self._resolve_session(mounted)

    def _start_box(self) -> None:
        self.tail.set("sandbox starting…")
        try:
            box.start(self.cfg, on_line=self._box_line, env=self.env)
            waited = box.wait_healthy(self.client.healthy, timeout=120.0,
                                      on_tick=lambda s: self.tail.set(
                                          f"sandbox starting… {s:.0f}s"),
                                      sleep=self._sleep)
        except box.BoxError as exc:
            self.tail.clear()
            raise Exit(EXIT_ENV, f"{self.pal('✗', 'error')} {exc}") from exc
        self.tail.clear()
        self.ok("sandbox ready", f"{waited:.1f}s")

    def _box_line(self, line: str) -> None:
        """docker's own chatter: pulls are worth showing, the rest is noise."""
        lower = line.lower()
        if any(word in lower for word in ("pulling", "download", "extract", "waiting")):
            self.tail.set(line.strip()[:120])
        elif self.cfg.verbosity > 0 and line.strip():
            self.say(self.pal(f"  {line.strip()}", "dim"))

    def _ensure_mounted(self) -> bool:
        """Make this folder visible inside the box, asking once if it is not.

        Returns whether the folder is visible — the caller needs to know,
        because pinning a chat to a path the container cannot see would point
        the agent at nothing.
        """
        host = paths.normalize_host(str(self.cwd))
        visible = [*mountlist.effective(self.cfg.mounts_file, approvals_file(self.env)),
                   str(self.cfg.config_dir)]
        if paths.covering_mount(host, visible) is not None:
            return True
        target = _git_root(self.cwd) or host
        why = paths.mount_refusal(target)
        if why is not None:
            self.warn(f"this folder cannot be mounted: it {why}")
            self.warn("the chat will run in the sandbox's own workspace instead")
            return False
        if not self._ask_mount(target):
            self.warn("not mounted — the chat runs in the sandbox's own workspace")
            return False
        mountlist.add(self.cfg.mounts_file, approvals_file(self.env), target, approve=True)
        self._recreate_for_mount(target)
        return True

    def _ask_mount(self, target: str) -> bool:
        if self.cfg.auto_mount:
            return True
        if not self.interactive_stdin:
            self.warn(f"{target} is not mounted; re-run in a terminal, or "
                      f"`aiforge mount add {target}`")
            return False
        self.say(f"{self.pal('!', 'warn')} {self.pal(target, 'head')} is not visible "
                 f"inside the sandbox.",
                 f"  Mount it? The agent gets full access to it, and the box restarts "
                 f"({self.pal('~4s', 'dim')}).")
        return self.ask(f"  [{self.pal('Y', 'ok')}/n] ").lower() in ("", "y", "yes")

    def _recreate_for_mount(self, target: str) -> None:
        busy = self._busy_sessions()
        if busy and not self.force:
            ids = ", ".join(f"#{s}" for s in busy)
            raise Exit(EXIT_ENV,
                       f"{self.pal('✗', 'error')} a run is in flight ({ids}); the sandbox "
                       f"cannot be restarted under it.\n"
                       f"  Watch it:  aiforge attach {busy[0]}\n"
                       f"  Or force:  aiforge --force mount add {target}   "
                       f"(the run is lost)")
        self.tail.set("restarting the sandbox with the new mount…")
        try:
            box.start(self.cfg, on_line=self._box_line, recreate=True, env=self.env)
            box.wait_healthy(self.client.healthy, timeout=120.0, sleep=self._sleep)
        except box.BoxError as exc:
            self.tail.clear()
            raise Exit(EXIT_ENV, f"{self.pal('✗', 'error')} {exc}") from exc
        self.tail.clear()
        self.ok(f"mounted {target}")

    def _busy_sessions(self) -> list[int]:
        """Sessions with a run in flight.

        The session list carries no such field, so each one is asked over its
        attach stream (first event only). Recreating the container under a live
        run loses that run, so this guard has to be real rather than cheap.
        """
        out: list[int] = []
        for row in self._sessions_safe()[:25]:
            sid = row.get("id")
            if isinstance(sid, int) and self.client.is_running(sid):
                out.append(sid)
        return out

    def _sessions_safe(self) -> list[dict]:
        try:
            return self.client.sessions()
        except (api.ApiDown, api.Busy):
            return []

    def _resolve_session(self, mounted: bool = True) -> None:
        listed = self._sessions_safe()
        known = {int(s["id"]) for s in listed if str(s.get("id", "")).isdigit()}
        boxpath = paths.to_box(paths.normalize_host(str(self.cwd)))
        found = sessions.for_folder(self.cfg.sessions_file, boxpath, known)
        if found is not None:
            self.session_id = found
            row = next((s for s in listed if int(s.get("id", -1)) == found), {})
            self.ok(f"chat #{found}", str(row.get("title") or "").strip())
            return
        # An unmounted folder does not exist inside the container: pinning the
        # chat to it would point every tool at a missing path. Let the API give
        # the session its own workspace instead.
        created = self.client.create_session(boxpath if mounted else None)
        self.session_id = int(created["id"])
        if mounted:
            sessions.remember(self.cfg.sessions_file, boxpath, self.session_id)
        where = paths.to_host(str(created.get("cwd") or boxpath))
        self.ok(f"chat #{self.session_id}", where)

    # ── one turn ───────────────────────────────────────────────────────────

    def send(self, message: str, *, quick: bool = False) -> int:
        """Run one turn and render it. Returns a process exit status."""
        if self.session_id is None:
            raise Exit(EXIT_USAGE, "no chat to send to")
        self.render.begin_turn()
        self.say("", self.render.user_line(message))
        return self._consume(
            lambda: self.client.send(self.session_id, message, mode=self.mode,
                                     quick=quick, review_edits=self.review_edits,
                                     role=self.role))

    def attach(self, session_id: int) -> int:
        """Watch a run that belongs to another producer.

        The session id is adopted first: every key the watcher handles (stop,
        steer) and every reconnect uses it, and using the folder's own session
        here stopped somebody else's run.
        """
        self.session_id = session_id
        self.render.begin_turn()
        self.render.begin_replay()
        return self._consume(lambda: self.client.attach(session_id), detach_only=True)

    def _consume(self, open_stream: Callable[[], object], *,
                 detach_only: bool = False) -> int:
        """Render an event stream, handling keys and reconnects as it goes.

        Takes a factory rather than an iterator: opening the stream is itself a
        request that can fail (a 500, a sandbox that just went away), and that
        failure deserves the same reconnect path as one that arrives mid-run.
        """
        assert self.session_id is not None
        status = EXIT_OK
        interrupts = 0
        reconnects = 0
        steer: list[str] = []
        last_spin = 0.0
        stream: object | None = None
        with KeyWatcher() as kb:
            while True:
                try:
                    if stream is None:
                        stream = open_stream()
                    for event in stream:
                        if self.cfg.json_events:
                            self.out.write(json.dumps(event) + "\n")
                            self.out.flush()
                            if event.get("type") == "error":
                                status = EXIT_AGENT
                            finished = event.get("type") in ("done", "stopped")
                        else:
                            status, finished = self._apply(event, status, kb)
                        now = time.monotonic()
                        if now - last_spin > 0.12:
                            self.tail.spin()
                            last_spin = now
                        interrupts, steer = self._keys(kb, interrupts, steer, detach_only)
                        if finished:
                            break
                    break
                except (api.Stalled, api.ApiDown, api.Busy) as exc:
                    reconnects += 1
                    if reconnects > MAX_RECONNECTS:
                        self.warn(f"gave up re-attaching after {MAX_RECONNECTS} tries "
                                  f"({exc})")
                        self.warn("aiforge box logs --tail 50   shows what the sandbox saw")
                        self.tail.clear()
                        return EXIT_ENV
                    wait = RECONNECT_BACKOFF[min(reconnects - 1, len(RECONNECT_BACKOFF) - 1)]
                    self.warn(f"stream dropped ({exc}) — re-attaching in {wait:.0f}s "
                              f"({reconnects}/{MAX_RECONNECTS})")
                    self._sleep(wait)
                    self.render.begin_replay()
                    # Re-open INSIDE the try on the next pass: calling attach()
                    # here put the retry's own failure outside the handler that
                    # exists to absorb it.
                    stream = None
                    open_stream = _attach_to(self.client, self.session_id)
                except KeyboardInterrupt:
                    interrupts += 1
                    if detach_only:
                        self.tail.clear()
                        self.say(self.pal("detached — the run keeps going", "dim"))
                        return EXIT_INTERRUPT
                    if interrupts >= 2:
                        self._kill_all()
                        self.tail.clear()
                        return EXIT_INTERRUPT
                    self._stop_run()
                    self.warn("stopping — Ctrl+C again to reset everything")
        self.tail.clear()
        return status

    def _apply(self, event: dict, status: int, kb: KeyWatcher) -> tuple[int, bool]:
        op = self.render.handle(event)
        if op.lines:
            self.tail.write(op.lines)
        if op.stream:
            self.tail.stream(op.stream)
        if op.tail is not None:
            self.tail.set(op.tail or None)
        if op.approval is not None:
            self._answer_approval(op.approval, kb)
        if event.get("type") == "error":
            status = EXIT_AGENT
        return status, op.finished

    def _keys(self, kb: KeyWatcher, interrupts: int, steer: list[str],
              detach_only: bool = False) -> tuple[int, list[str]]:
        """Esc stops, typing steers, Ctrl+C twice resets everything."""
        while True:
            key = kb.get()
            if key is None:
                return interrupts, steer
            if key == ESC:
                if detach_only:
                    self.warn("attached read-only — Ctrl+C to detach")
                    continue
                self.warn("stopping…")
                self._stop_run()
            elif key == CTRL_C:
                interrupts += 1
                if interrupts >= 2:
                    self._kill_all()
                    self.warn("everything reset")
                else:
                    self._stop_run()
                    self.warn("stopping — Ctrl+C again to reset everything")
            elif key == ENTER and steer:
                text = "".join(steer).strip()
                steer = []
                if text:
                    self._steer(text)
            elif key in ("\x7f", "\b"):
                steer = steer[:-1]
                self.tail.set(f"steer: {''.join(steer)}  (enter to send)"
                              if steer else None)
            elif key.isprintable():
                if detach_only:
                    continue
                steer.append(key)
                self.tail.set(f"steer: {''.join(steer)}  (enter to send)")

    def _answer_approval(self, event: dict, kb: KeyWatcher | None = None) -> None:
        """The one place the CLI blocks on the user mid-run.

        The key watcher is suspended first: two readers on one fd race for
        every byte, so the answer used to be eaten as steer text while the
        prompt blocked. The default is REJECT — an empty answer, a closed
        stdin, or a stray byte must never approve a file write.
        """
        assert self.session_id is not None
        if self._approve_all:
            self._approve(event, "allow")
            return
        if not self.interactive_stdin:
            self._approve(event, "reject", "no terminal to ask — rejected")
            self.warn("rejected (nothing to ask on)")
            return
        if kb is not None:
            kb.pause()
        try:
            answer = self.ask(f"  {self.pal('[a]', 'ok')}llow  "
                              f"{self.pal('[r]', 'fail')}eject (default)  "
                              f"{self.pal('[A]', 'ok')}llow all in this chat: ")
        finally:
            if kb is not None:
                kb.resume()
        if answer == "A":
            self._approve_all = True
        decision = "allow" if answer.lower().startswith("a") else "reject"
        self._approve(event, decision)
        self.say(f"  {self.pal(decision, 'ok' if decision == 'allow' else 'fail')}")

    def _approve(self, event: dict, decision: str, note: str | None = None) -> None:
        try:
            self.client.approve(self.session_id, event.get("id"), decision, note)
        except (api.ApiDown, api.Busy) as exc:
            self.warn(f"could not send the decision: {exc}")

    def _steer(self, text: str) -> None:
        try:
            self.client.steer(self.session_id, text)   # type: ignore[arg-type]
            self.ok("steer sent", text[:60])
        except (api.ApiDown, api.Busy) as exc:
            self.warn(f"steer failed: {exc}")

    def _stop_run(self) -> None:
        with contextlib.suppress(api.ApiDown, api.Busy):
            self.client.stop(self.session_id)          # type: ignore[arg-type]

    def _kill_all(self) -> None:
        with contextlib.suppress(api.ApiDown, api.Busy):
            self.client.kill_all()

    # ── the loop ───────────────────────────────────────────────────────────

    def interactive(self) -> int:
        from .input import ChatCompleter, build_session
        completer = ChatCompleter(models=self._model_choices, sessions=self._session_choices)
        session = build_session(self.cfg.history_file, completer)
        self.say(self.pal("/help for commands · esc stops a run · ctrl+d exits", "dim"), "")
        status = EXIT_OK
        interrupts = 0
        while True:
            try:
                text = session.prompt("▸ ").strip()
                interrupts = 0
            except KeyboardInterrupt:
                interrupts += 1
                if interrupts >= 2:
                    self._kill_all()
                    self.warn("everything reset")
                    interrupts = 0
                continue
            except EOFError:
                return status
            if not text:
                continue
            if text.startswith("/"):
                handled, leave_with = self.slash(text)
                if handled:
                    if leave_with is not None:
                        return leave_with
                    continue
            status = self.send(text)

    def slash(self, text: str) -> tuple[bool, int | None]:
        """Client-side commands. Anything unknown goes to the agent, which
        resolves user-defined commands from .aiforge/commands/*.md."""
        parts = text.split()
        name, args = parts[0], parts[1:]
        if tbl.by_name(name, tbl.SLASH) is None:
            return False, None
        if name == "/exit":
            return True, EXIT_OK
        if name == "/help":
            self.say(helptext.command_help(self.pal, args[0]) if args
                     else helptext.slash_help(self.pal))
        elif name == "/mode":
            if args and args[0] in tbl.MODES:
                self.mode = args[0]
                self.ok(f"mode {self.mode}")
            else:
                self.say(f"  mode {self.pal(self.mode, 'head')}   "
                         f"{self.pal('/mode ' + '|'.join(tbl.MODES), 'dim')}")
        elif name == "/model":
            self.say(*self._model_command(args))
        elif name == "/review-edits":
            self.review_edits = bool(args and args[0] == "on")
            self.ok(f"review-edits {'on' if self.review_edits else 'off'}")
        elif name == "/quick":
            if args:
                self.send(" ".join(args), quick=True)
        elif name == "/new":
            self._resolve_new_session()
        elif name == "/sessions":
            self.say(*_session_lines(self._sessions_safe(), self.pal))
        elif name == "/resume":
            if args and args[0].isdigit():
                self.session_id = int(args[0])
                sessions.remember(self.cfg.sessions_file,
                                  paths.to_box(paths.normalize_host(str(self.cwd))),
                                  self.session_id)
                self.ok(f"chat #{self.session_id}")
        elif name == "/stop":
            self._stop_run()
            self.ok("stopped")
        elif name == "/compact":
            self.client.compact(self.session_id)          # type: ignore[arg-type]
            self.ok("history folded into a summary")
        elif name == "/ctx":
            self.say(*self._ctx_lines())
        elif name == "/integrations":
            self.say(*self.integrations_command(args or ["ls"]))
        elif name in ("/mounts", "/mount"):
            self.say(*self.mount_command(args))
        elif name == "/cd":
            if args:
                self.cwd = Path(paths.normalize_host(args[0], cwd=str(self.cwd)))
                mounted = self._ensure_mounted()
                self._resolve_session(mounted)
        elif name == "/box":
            self.box_command(args)
        return True, None

    def _resolve_new_session(self) -> None:
        boxpath = paths.to_box(paths.normalize_host(str(self.cwd)))
        created = self.client.create_session(boxpath)
        self.session_id = int(created["id"])
        sessions.remember(self.cfg.sessions_file, boxpath, self.session_id)
        self.ok(f"chat #{self.session_id}")

    def _model_command(self, args: list[str]) -> list[str]:
        """Show the models the sandbox has, or pin this chat to one.

        The model is the session's ROLE server-side, and the message route
        switches it when a turn carries a different one — so this records the
        choice and the next turn applies it.
        """
        choices = self._model_choices()
        if not args:
            out = [f"  model {self.pal(self.role or 'chat (default)', 'head')}"]
            for value, meta in choices[:20]:
                mark = "✓" if value == self.role else "·"
                out.append(f"  {self.pal(mark, 'ok' if mark == '✓' else 'dim')} {value}"
                           f"   {self.pal(meta, 'dim')}")
            return out or [self.pal("the sandbox reported no models", "dim")]
        wanted = args[0]
        known = [value for value, _ in choices]
        if known and wanted not in known:
            return [f"{self.pal('✗', 'fail')} no model '{wanted}' here",
                    self.pal("  " + ", ".join(known[:12]), "dim")]
        self.role = wanted
        return [f"{self.pal('✓', 'ok')} model {wanted} "
                f"{self.pal('(applies from the next turn)', 'dim')}"]

    def _ctx_lines(self) -> list[str]:
        if self.session_id is None:
            return [self.pal("no chat yet", "dim")]
        try:
            usage = self.client.llm_usage(self.session_id)
        except (api.ApiDown, api.Busy):
            usage = {}
        out = [f"  chat  {self.pal('#' + str(self.session_id), 'head')}"
               f"   mode {self.pal(self.mode, 'head')}"
               f"   model {self.pal(self.role or 'chat', 'head')}"]
        # /llm-usage answers in request counts (turn / session / per_minute);
        # the context percentage only exists on the stream's `usage` event.
        counts = [f"{key} {usage[key]}" for key in ("turn", "session", "per_minute")
                  if isinstance(usage.get(key), int)]
        if counts:
            out.append("  llm   " + self.pal("  ".join(counts), "dim"))
        return out

    # ── commands shared with the top level ─────────────────────────────────

    def mount_command(self, args: list[str]) -> list[str]:
        action = args[0] if args else "ls"
        if action in ("add", "rm", "approve") and len(args) >= 2:
            target = paths.normalize_host(args[1], cwd=str(self.cwd))
            if action == "rm":
                mountlist.remove(self.cfg.mounts_file, approvals_file(self.env), target)
                return [f"{self.pal('✓', 'ok')} unlisted {target} "
                        f"{self.pal('(effective after a box restart)', 'dim')}"]
            why = paths.mount_refusal(target)
            if why is not None:
                return [f"{self.pal('✗', 'fail')} {target} {why}"]
            mountlist.add(self.cfg.mounts_file, approvals_file(self.env), target,
                          approve=True)
            self._recreate_for_mount(target)
            return []
        out = [self.pal("mounted", "head")]
        for m in mountlist.effective(self.cfg.mounts_file, approvals_file(self.env)):
            out.append(f"  {self.pal('✓', 'ok')} {m}")
        out.append(f"  {self.pal('✓', 'ok')} {self.cfg.config_dir} "
                   f"{self.pal('(always)', 'dim')}")
        waiting = mountlist.pending(self.cfg.mounts_file, approvals_file(self.env))
        if waiting:
            out.append(self.pal("waiting for your approval", "head"))
            out += [f"  {self.pal('·', 'warn')} {m}   "
                    f"{self.pal('aiforge mount approve ' + m, 'dim')}" for m in waiting]
        return out

    def integrations_command(self, args: list[str]) -> list[str]:
        """Read, change or prove the Jira/Confluence/GitLab/email settings.

        Kept deliberately narrow: the agent already has these as tools, so the
        only thing the terminal adds is the configuration a human owns.
        """
        action = args[0] if args else "ls"
        if action not in integ.KINDS and action not in tbl.INTEGRATION_ACTIONS:
            return [f"{self.pal('✗', 'fail')} integrations: unknown action '{action}' "
                    f"({', '.join(tbl.INTEGRATION_ACTIONS)})"]
        # `integrations jira` is the obvious shorthand for `integrations get jira`.
        if action in integ.KINDS:
            args = ["get", action]
            action = "get"
        kinds = [args[1]] if len(args) > 1 and args[1] in integ.KINDS else list(integ.KINDS)
        if action == "ls":
            out = [self.pal("integrations", "head")]
            for kind in kinds:
                rows = dict(integ.summary(kind, self._integration_safe(kind)))
                state = rows.get("has_token") or rows.get("has_smtp_password") or "not set"
                where = rows.get("base_url") or rows.get("smtp_host") or ""
                mark = "ok" if state == "configured" else "dim"
                out.append(f"  {self.pal('•', mark)} {kind.ljust(11)}"
                           f"{self.pal(state, mark)}   {self.pal(where, 'dim')}")
            out.append(self.pal("  aiforge integrations get <kind> for the detail", "dim"))
            return out
        if action == "get":
            out = []
            for kind in kinds:
                out.append(self.pal(kind, "head"))
                for key, value in integ.summary(kind, self._integration_safe(kind)):
                    out.append(f"  {key.ljust(18)} {self.pal(value, 'dim')}")
            return out
        if action == "test":
            out = []
            for kind in kinds:
                try:
                    result = self.client.integration_test(kind)
                except (api.ApiDown, api.Busy) as exc:
                    out.append(f"{self.pal('✗', 'fail')} {kind}: {exc}")
                    continue
                good = bool(result.get("ok") or result.get("success"))
                detail = str(result.get("detail") or result.get("message")
                             or result.get("error") or "").strip()
                out.append(f"{self.pal('✓' if good else '✗', 'ok' if good else 'fail')} "
                           f"{kind} {self.pal(detail[:120], 'dim')}")
            return out
        # set
        if len(args) < 2 or args[1] not in integ.KINDS:
            return [f"{self.pal('✗', 'fail')} which one? "
                    f"aiforge integrations set <{'|'.join(integ.KINDS)}> key=value …"]
        kind = args[1]
        try:
            patch = integ.parse_assignments(args[2:])
            integ.check_keys(kind, patch)
        except ValueError as exc:
            return [f"{self.pal('✗', 'fail')} {exc}"]
        if not patch:
            return [f"{self.pal('✗', 'fail')} nothing to set — pass key=value pairs"]
        saved = self.client.integration_set(kind, patch)
        changed = ", ".join(k for k in patch if not integ.is_secret(k))
        secrets = [k for k in patch if integ.is_secret(k)]
        note = " ".join(x for x in (changed, "+secret" if secrets else "") if x)
        return [f"{self.pal('✓', 'ok')} {kind} saved   {self.pal(note, 'dim')}",
                *[f"  {k.ljust(18)} {self.pal(v, 'dim')}"
                  for k, v in integ.summary(kind, saved)]]

    def _integration_safe(self, kind: str) -> dict:
        try:
            return self.client.integration(kind)
        except (api.ApiDown, api.Busy):
            return {}

    def box_command(self, args: list[str], *, tail: int = 200,
                    follow: bool = False) -> int:
        action = args[0] if args else "status"
        try:
            return self._box_action(action, tail=tail, follow=follow)
        except box.BoxError as exc:
            # These are the commands people run BECAUSE the box is broken, so a
            # traceback here is the worst possible answer.
            raise Exit(EXIT_ENV, f"{self.pal('✗', 'error')} {exc}") from exc

    def _box_action(self, action: str, *, tail: int, follow: bool) -> int:
        if action == "status":
            strategy = f"run.sh {self.cfg.repo}" if self.cfg.repo else "compose"
            exe = box.docker_bin()
            state = box.container_state(exe) if exe else "no docker"
            healthy = self.client.healthy()
            self.say(f"  container {self.pal(state, 'ok' if state == 'running' else 'warn')}",
                     f"  api       "
                     f"{self.pal('up' if healthy else 'down', 'ok' if healthy else 'fail')}"
                     f"   {self.pal(self.cfg.base_url, 'dim')}",
                     f"  image     {self.pal(self.cfg.image, 'dim')}",
                     f"  strategy  {self.pal(strategy, 'dim')}")
            return EXIT_OK if healthy else EXIT_ENV
        if action in ("up", "restart"):
            box.start(self.cfg, on_line=self._box_line, recreate=action == "restart",
                      env=self.env)
            box.wait_healthy(self.client.healthy, timeout=120.0, sleep=self._sleep)
            self.ok(f"sandbox {action}")
            return EXIT_OK
        if action == "down":
            box.stop(self.cfg)
            self.ok("sandbox stopped")
            return EXIT_OK
        if action == "logs":
            return box.logs(self.cfg, tail=tail, follow=follow)
        if action == "shell":
            return box.shell(self.cfg)
        self.warn(f"box: unknown action '{action}' ({', '.join(tbl.BOX_ACTIONS)})")
        return EXIT_USAGE

    # ── completion sources ─────────────────────────────────────────────────

    def _model_choices(self) -> list[tuple[str, str]]:
        try:
            data = self.client.models()
        except (api.ApiDown, api.Busy):
            return []
        rows = data.get("models", data) if isinstance(data, dict) else data
        out = []
        for row in rows or []:
            if isinstance(row, str):
                out.append((row, ""))
            elif isinstance(row, dict):
                out.append((str(row.get("id") or row.get("name") or ""),
                            str(row.get("role") or row.get("provider") or "")))
        return [r for r in out if r[0]]

    def _session_choices(self) -> list[tuple[str, str]]:
        return [(str(s.get("id")), str(s.get("title") or "")) for s in self._sessions_safe()]


def _git_root(start: Path) -> str | None:
    """The repo this folder belongs to — the more useful thing to mount.

    Read from the filesystem, not from `git`, because the host may not have git
    installed (the binary does not require it).
    """
    for candidate in [start, *start.parents]:
        if (candidate / ".git").exists():
            return str(candidate)
    return None


def _session_lines(rows: list[dict], pal: Palette) -> list[str]:
    if not rows:
        return [pal("no chats yet", "dim")]
    out = [pal("chats", "head")]
    for row in rows[:20]:
        sid = row.get("id")
        title = str(row.get("title") or "").strip() or "(untitled)"
        cwd = paths.to_host(str(row.get("cwd") or ""))
        count = row.get("message_count")
        note = f"{count} msgs" if isinstance(count, int) else ""
        out.append(f"  {pal(f'#{sid}', 'head')} {title}   {pal(cwd, 'dim')} "
                   f"{pal(note, 'dim')}")
    return out
