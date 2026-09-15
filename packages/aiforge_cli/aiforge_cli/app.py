"""Boot the sandbox, then talk to it until the user leaves.

The order here is the whole user-visible contract: nothing is asked except a
new mount, and everything else — image, container, health, session — is made to
exist quietly. Rendering decisions live in render.py, terminal control in
tail.py; this module is the sequence.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import box, client as api, commands as tbl, help as helptext
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


class Exit(Exception):
    """Leave with this status. The message, if any, is already printed."""

    def __init__(self, code: int, message: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message


class App:
    def __init__(self, cfg: Config, pal: Palette, *, cwd: Path | None = None,
                 out=None, env: dict[str, str] | None = None):
        self.cfg = cfg
        self.pal = pal
        self.cwd = Path.cwd() if cwd is None else cwd
        self.out = out or sys.stdout
        self.env = env
        self.client = api.Client(cfg.base_url)
        self.tail = Tail(self.out, pal=pal)
        self.render = Renderer(pal, verbosity=cfg.verbosity)
        self.session_id: int | None = None
        self.mode = "simple"
        self.review_edits = False
        self._approve_all = False

    # ── output helpers ─────────────────────────────────────────────────────

    def say(self, *lines: str) -> None:
        self.tail.write(list(lines))

    def ok(self, text: str, note: str = "") -> None:
        suffix = f"   {self.pal(note, 'dim')}" if note else ""
        self.say(f"{self.pal('✓', 'ok')} {text}{suffix}")

    def warn(self, text: str) -> None:
        self.say(f"{self.pal('!', 'warn')} {text}")

    # ── boot ───────────────────────────────────────────────────────────────

    def boot(self) -> None:
        if not self.client.healthy():
            self._start_box()
        self._ensure_mounted()
        self._resolve_session()

    def _start_box(self) -> None:
        self.tail.set("sandbox starting…")
        try:
            box.start(self.cfg, on_line=self._box_line, env=self.env)
            waited = box.wait_healthy(self.client.healthy, timeout=120.0,
                                      on_tick=lambda s: self.tail.set(
                                          f"sandbox starting… {s:.0f}s"))
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

    def _ensure_mounted(self) -> None:
        """Make this folder visible inside the box, asking once if it is not."""
        host = paths.normalize_host(str(self.cwd))
        visible = mountlist.effective(self.cfg.mounts_file, approvals_file(self.env))
        visible = [*visible, str(self.cfg.config_dir)]
        if paths.covering_mount(host, visible) is not None:
            return
        target = _git_root(self.cwd) or host
        why = paths.mount_refusal(target)
        if why is not None:
            self.warn(f"this folder cannot be mounted: it {why}")
            self.warn("the chat will run in the sandbox's own workspace instead")
            return
        if not self._ask_mount(target):
            self.warn("not mounted — the chat runs in the sandbox's own workspace")
            return
        mountlist.add(self.cfg.mounts_file, approvals_file(self.env), target, approve=True)
        self._recreate_for_mount(target)

    def _ask_mount(self, target: str) -> bool:
        if self.cfg.auto_mount:
            return True
        if not sys.stdin.isatty():
            self.warn(f"{target} is not mounted; re-run in a terminal, or `aiforge mount add "
                      f"{target}`")
            return False
        self.say(f"{self.pal('!', 'warn')} {self.pal(target, 'head')} is not visible inside "
                 f"the sandbox.",
                 f"  Mount it? The agent gets full access to it, and the box restarts "
                 f"({self.pal('~4s', 'dim')}).")
        answer = input(f"  [{self.pal('Y', 'ok')}/n] ").strip().lower()
        return answer in ("", "y", "yes")

    def _recreate_for_mount(self, target: str) -> None:
        busy = [s for s in self._sessions_safe() if s.get("running")]
        if busy:
            ids = ", ".join(f"#{s.get('id')}" for s in busy)
            raise Exit(EXIT_ENV,
                       f"{self.pal('✗', 'error')} a run is in flight ({ids}); the sandbox "
                       f"cannot be restarted under it.\n"
                       f"  Stop it first, or `aiforge attach {busy[0].get('id')}` to watch it.")
        self.tail.set("restarting the sandbox with the new mount…")
        try:
            box.start(self.cfg, on_line=self._box_line, recreate=True, env=self.env)
            box.wait_healthy(self.client.healthy, timeout=120.0)
        except box.BoxError as exc:
            self.tail.clear()
            raise Exit(EXIT_ENV, f"{self.pal('✗', 'error')} {exc}") from exc
        self.tail.clear()
        self.ok(f"mounted {target}")

    def _sessions_safe(self) -> list[dict]:
        try:
            return self.client.sessions()
        except (api.ApiDown, api.Busy):
            return []

    def _resolve_session(self) -> None:
        listed = self._sessions_safe()
        known = {int(s["id"]) for s in listed if str(s.get("id", "")).isdigit()}
        boxpath = paths.to_box(paths.normalize_host(str(self.cwd)))
        found = sessions.for_folder(self.cfg.sessions_file, boxpath, known)
        if found is not None:
            self.session_id = found
            row = next((s for s in listed if int(s.get("id", -1)) == found), {})
            self.ok(f"chat #{found}", str(row.get("title") or "").strip())
            return
        created = self.client.create_session(boxpath)
        self.session_id = int(created["id"])
        sessions.remember(self.cfg.sessions_file, boxpath, self.session_id)
        self.ok(f"chat #{self.session_id}", f"{paths.to_host(boxpath)}")

    # ── one turn ───────────────────────────────────────────────────────────

    def send(self, message: str) -> int:
        """Run one turn and render it. Returns a process exit status."""
        assert self.session_id is not None
        self.render.begin_turn()
        self.say("", self.render.user_line(message))
        stream = self.client.send(self.session_id, message, mode=self.mode,
                                  review_edits=self.review_edits)
        return self._consume(stream)

    def attach(self, session_id: int) -> int:
        self.render.begin_turn()
        self.render.begin_replay()
        return self._consume(self.client.attach(session_id))

    def _consume(self, stream) -> int:
        """Render an event stream, handling keys and reconnects as it goes."""
        assert self.session_id is not None
        status = EXIT_OK
        interrupts = 0
        steer: list[str] = []
        last_spin = 0.0
        with KeyWatcher() as kb:
            while True:
                try:
                    for event in stream:
                        if self.cfg.json_events:
                            self.out.write(json.dumps(event) + "\n")
                            self.out.flush()
                        else:
                            status = self._apply(event, status)
                        now = time.monotonic()
                        if now - last_spin > 0.12:
                            self.tail.spin()
                            last_spin = now
                        interrupts, steer = self._keys(kb, interrupts, steer)
                        if event.get("type") in ("done", "stopped"):
                            break
                    break
                except api.Stalled as exc:
                    self.warn(f"stream dropped ({exc}) — re-attaching")
                    self.render.begin_replay()
                    stream = self.client.attach(self.session_id)
                except api.Busy:
                    self.warn("a run is already in flight — attaching to it")
                    self.render.begin_replay()
                    stream = self.client.attach(self.session_id)
                except KeyboardInterrupt:
                    self._stop_run()
                    self.tail.clear()
                    return EXIT_INTERRUPT
        self.tail.clear()
        return status

    def _apply(self, event: dict, status: int) -> int:
        op = self.render.handle(event)
        if op.lines:
            self.tail.write(op.lines)
        if op.stream:
            self.tail.stream(op.stream)
        if op.tail is not None:
            self.tail.set(op.tail or None)
        if op.approval is not None:
            self._answer_approval(op.approval)
        if event.get("type") == "error":
            return EXIT_AGENT
        return status

    def _keys(self, kb: KeyWatcher, interrupts: int, steer: list[str]) -> tuple[int, list[str]]:
        """Esc stops, typing steers, Ctrl+C twice kills everything."""
        while True:
            key = kb.get()
            if key is None:
                return interrupts, steer
            if key == ESC:
                self.warn("stopping…")
                self._stop_run()
            elif key == CTRL_C:
                interrupts += 1
                if interrupts == 1:
                    self._stop_run()
                    self.warn("stopping — Ctrl+C again to reset everything")
                else:
                    self.client.kill_all()
                    self.warn("everything reset")
            elif key == ENTER and steer:
                text = "".join(steer).strip()
                steer = []
                if text:
                    self.client.steer(self.session_id, text)
                    self.ok("steer sent", text[:60])
            elif key in ("\x7f", "\b"):
                steer = steer[:-1]
            elif key.isprintable():
                steer.append(key)
                self.tail.set(f"steer: {''.join(steer)}  (enter to send)")

    def _answer_approval(self, event: dict) -> None:
        """The one place the CLI blocks on the user mid-run."""
        assert self.session_id is not None
        if self._approve_all:
            self.client.approve(self.session_id, event.get("id"), "allow")
            return
        if not sys.stdin.isatty():
            self.client.approve(self.session_id, event.get("id"), "reject",
                               "no terminal to ask — rejected")
            self.warn("rejected (nothing to ask on)")
            return
        prompt = (f"  {self.pal('[a]', 'ok')}llow  {self.pal('[r]', 'fail')}eject  "
                  f"{self.pal('[A]', 'ok')}llow all in this chat: ")
        try:
            answer = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            answer = "r"
        if answer == "A":
            self._approve_all = True
        decision = "reject" if answer.lower().startswith("r") else "allow"
        self.client.approve(self.session_id, event.get("id"), decision)
        self.say(f"  {self.pal(decision, 'ok' if decision == 'allow' else 'fail')}")

    def _stop_run(self) -> None:
        try:
            self.client.stop(self.session_id)          # type: ignore[arg-type]
        except (api.ApiDown, api.Busy):
            pass

    # ── the loop ───────────────────────────────────────────────────────────

    def interactive(self) -> int:
        from .input import ChatCompleter, build_session
        completer = ChatCompleter(models=self._model_choices, sessions=self._session_choices)
        session = build_session(self.cfg.history_file, completer)
        self.say(self.pal("/help for commands · esc stops a run · ctrl+d exits", "dim"), "")
        status = EXIT_OK
        while True:
            try:
                text = session.prompt("▸ ").strip()
            except KeyboardInterrupt:
                continue
            except EOFError:
                return status
            if not text:
                continue
            if text.startswith("/"):
                handled, status_or_none = self.slash(text)
                if handled:
                    if status_or_none is not None:
                        return status_or_none
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
        elif name == "/review-edits":
            self.review_edits = bool(args and args[0] == "on")
            self.ok(f"review-edits {'on' if self.review_edits else 'off'}")
        elif name == "/quick":
            if args:
                self.render.begin_turn()
                self.say("", self.render.user_line(" ".join(args)))
                self._consume(self.client.send(self.session_id, " ".join(args),  # type: ignore[arg-type]
                                               mode=self.mode, quick=True,
                                               review_edits=self.review_edits))
        elif name == "/new":
            boxpath = paths.to_box(paths.normalize_host(str(self.cwd)))
            created = self.client.create_session(boxpath)
            self.session_id = int(created["id"])
            sessions.remember(self.cfg.sessions_file, boxpath, self.session_id)
            self.ok(f"chat #{self.session_id}")
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
            self.say(*_ctx_lines(self.client, self.session_id, self.mode, self.pal))
        elif name == "/integrations":
            self.say(*self.integrations_command(args or ["ls"]))
        elif name in ("/mounts", "/mount"):
            self.say(*self.mount_command(args))
        elif name == "/cd":
            if args:
                self.cwd = Path(paths.normalize_host(args[0], cwd=str(self.cwd)))
                self._ensure_mounted()
                self._resolve_session()
        elif name == "/box":
            self.box_command(args)
        return True, None

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
            mountlist.add(self.cfg.mounts_file, approvals_file(self.env), target, approve=True)
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
                state = rows.get("has_token") or rows.get("has_password") or "not set"
                where = rows.get("base_url") or rows.get("host") or ""
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
                    out.append(f"  {key.ljust(16)} {self.pal(value, 'dim')}")
            return out
        if action == "test":
            out = []
            for kind in kinds:
                try:
                    result = self.client.integration_test(kind)
                except api.ApiDown as exc:
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
        try:
            patch = integ.parse_assignments(args[2:])
        except ValueError as exc:
            return [f"{self.pal('✗', 'fail')} {exc}"]
        if not patch:
            return [f"{self.pal('✗', 'fail')} nothing to set — pass key=value pairs"]
        saved = self.client.integration_set(args[1], patch)
        changed = ", ".join(k for k in patch if not integ.is_secret(k))
        secrets = [k for k in patch if integ.is_secret(k)]
        note = " ".join(x for x in (changed, "+secret" if secrets else "") if x)
        return [f"{self.pal('✓', 'ok')} {args[1]} saved   {self.pal(note, 'dim')}",
                *[f"  {k.ljust(16)} {self.pal(v, 'dim')}"
                  for k, v in integ.summary(args[1], saved)]]

    def _integration_safe(self, kind: str) -> dict:
        try:
            return self.client.integration(kind)
        except (api.ApiDown, api.Busy):
            return {}

    def box_command(self, args: list[str]) -> int:
        action = args[0] if args else "status"
        if action == "status":
            exe = box.docker_bin()
            state = box.container_state(exe) if exe else "no docker"
            healthy = self.client.healthy()
            self.say(f"  container {self.pal(state, 'ok' if state == 'running' else 'warn')}",
                     f"  api       {self.pal('up' if healthy else 'down', 'ok' if healthy else 'fail')}"
                     f"   {self.pal(self.cfg.base_url, 'dim')}",
                     f"  image     {self.pal(self.cfg.image, 'dim')}",
                     f"  strategy  {self.pal('run.sh ' + str(self.cfg.repo) if self.cfg.repo else 'compose', 'dim')}")
            return EXIT_OK if healthy else EXIT_ENV
        if action in ("up", "restart"):
            box.start(self.cfg, on_line=self._box_line, recreate=action == "restart",
                      env=self.env)
            box.wait_healthy(self.client.healthy, timeout=120.0)
            self.ok(f"sandbox {action}")
            return EXIT_OK
        if action == "down":
            box.stop(self.cfg)
            self.ok("sandbox stopped")
            return EXIT_OK
        if action == "logs":
            tail = 200
            follow = False
            for i, a in enumerate(args[1:]):
                if a in ("-f", "--follow"):
                    follow = True
                elif a in ("--tail", "-n") and len(args) > i + 2:
                    tail = int(args[i + 2])
                elif a.startswith("--tail="):
                    tail = int(a.split("=", 1)[1])
            return box.logs(self.cfg, tail=tail, follow=follow)
        if action == "shell":
            return box.shell(self.cfg)
        self.warn(f"box: unknown action '{action}' ({', '.join(tbl.BOX_ACTIONS)})")
        return EXIT_USAGE

    # ── completion sources ─────────────────────────────────────────────────

    def _model_choices(self) -> list[tuple[str, str]]:
        data = self.client.models()
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
    out = [pal("chats", "head")]
    for row in rows[:20]:
        sid = row.get("id")
        title = str(row.get("title") or "").strip() or "(untitled)"
        cwd = paths.to_host(str(row.get("cwd") or ""))
        flag = pal(" running", "warn") if row.get("running") else ""
        out.append(f"  {pal(f'#{sid}', 'head')} {title}{flag}   {pal(cwd, 'dim')}")
    return out or [pal("no chats yet", "dim")]


def _ctx_lines(client: api.Client, session_id: int | None, mode: str,
               pal: Palette) -> list[str]:
    if session_id is None:
        return [pal("no chat yet", "dim")]
    try:
        usage = client.llm_usage(session_id)
    except Exception:  # noqa: BLE001 — a missing route is not worth an error here
        usage = {}
    pct = usage.get("pct")
    out = [f"  chat  {pal('#' + str(session_id), 'head')}   mode {pal(mode, 'head')}"]
    if isinstance(pct, (int, float)):
        out.append(f"  ctx   {pal(f'{pct:.0f}%', pal.ctx(float(pct)))}"
                   f"   {pal(str(usage.get('windowTokens') or '') + ' tokens', 'dim')}")
    return out
