"""The sequence: boot, ask nothing but a mount, stream, stop, attach.

Every question the CLI can ask goes through App.ask, and every server call
through a client object, so the whole user-visible contract is assertable with
no sandbox, no terminal and no network.
"""

from __future__ import annotations

import io
import json
import queue

import pytest
from aiforge_cli import client as api
from aiforge_cli.app import EXIT_AGENT, EXIT_ENV, EXIT_INTERRUPT, EXIT_OK, App, Exit
from aiforge_cli.colors import Palette
from aiforge_cli.config import Config
from aiforge_cli.keys import KeyWatcher

PLAIN = Palette(False)


class FakeClient:
    """Enough of client.Client to drive App, and a log of what was called."""

    def __init__(self, *, events=None, healthy=True, running=()):
        self.events = list(events or [{"type": "done"}])
        self._healthy = healthy
        self._running = set(running)
        self.calls: list[tuple] = []
        self.sessions_rows: list[dict] = []
        self.next_id = 7

    def healthy(self, timeout: float = 1.5) -> bool:
        return self._healthy

    def sessions(self):
        return list(self.sessions_rows)

    def is_running(self, session_id: int) -> bool:
        return session_id in self._running

    def create_session(self, box_cwd):
        self.calls.append(("create", box_cwd))
        row = {"id": self.next_id, "cwd": box_cwd or "/var/lib/aiforge/session-7"}
        self.next_id += 1
        self.sessions_rows.append(row)
        return row

    def send(self, session_id, content, **kw):
        self.calls.append(("send", session_id, content, kw))
        return iter(self.events)

    def attach(self, session_id):
        self.calls.append(("attach", session_id))
        return iter(self.events)

    def stop(self, session_id):
        self.calls.append(("stop", session_id))
        return {}

    def steer(self, session_id, text):
        self.calls.append(("steer", session_id, text))
        return {}

    def approve(self, session_id, approval_id, decision, note=None):
        self.calls.append(("approve", session_id, approval_id, decision))
        return {}

    def kill_all(self):
        self.calls.append(("kill_all",))
        return {}

    def models(self):
        return ["chat", "qwen3.8-27b"]

    def llm_usage(self, session_id):
        return {"turn": 3, "session": 9, "per_minute": 2}

    def close(self):
        pass


def _app(tmp_path, client, *, answers=None, cwd=None, verbosity=0, json_events=False,
         auto_mount=False, interactive=True, err=None):
    cfg = Config(port=8799, config_dir=tmp_path / ".aiforge", repo=None,
                 image="aiforge-sandbox:local", auto_mount=auto_mount,
                 verbosity=verbosity, json_events=json_events)
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    replies = list(answers or [])
    work = cwd or (tmp_path / "work")
    work.mkdir(parents=True, exist_ok=True)
    app = App(cfg, PLAIN, cwd=work, out=io.StringIO(), err=err or io.StringIO(),
              env={"HOME": str(tmp_path)}, client=client,
              ask=lambda _p: replies.pop(0) if replies else "",
              interactive_stdin=interactive, sleep=lambda _s: None)
    return app


def _printed(app) -> str:
    """Everything the user saw — stdout plus the operator stream."""
    return app.out.getvalue() + app.msg_out.getvalue()


# ── boot ───────────────────────────────────────────────────────────────────


def test_boot_asks_exactly_one_question_the_mount(tmp_path):
    app = _app(tmp_path, FakeClient(), answers=["n"])
    app.boot()
    assert len(app._asked) == 1
    assert "[Y/n]" in app._asked[0]


def test_boot_asks_nothing_at_all_once_the_folder_is_mounted(tmp_path):
    client = FakeClient()
    app = _app(tmp_path, client, answers=["y"])
    work = app.cwd
    app.cfg.mounts_file.write_text(f"{work}\n")
    (tmp_path / ".config" / "aiforge").mkdir(parents=True)
    (tmp_path / ".config" / "aiforge" / "approved-mounts").write_text(f"{work}\n")
    app.env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / ".config")}
    app.boot()
    assert app._asked == []
    assert app.session_id == 7


def test_declining_the_mount_gives_the_chat_the_boxs_own_workspace(tmp_path):
    client = FakeClient()
    app = _app(tmp_path, client, answers=["n"])
    app.boot()
    # Pinning the chat to a path the container cannot see would point every
    # tool at nothing, so the cwd is left to the API.
    assert ("create", None) in client.calls


def test_a_folder_that_must_not_be_mounted_is_explained_not_offered(tmp_path,
                                                                    monkeypatch):
    home = tmp_path / "home"
    guarded = home / ".ssh"
    guarded.mkdir(parents=True)
    app = _app(tmp_path, FakeClient(), cwd=guarded)
    app.env = {"HOME": str(home)}
    # paths.mount_refusal resolves ~ itself, so HOME has to move with the
    # fixture — monkeypatch puts it back whatever the test does.
    monkeypatch.setenv("HOME", str(home))
    app.boot()
    assert app._asked == []
    assert "cannot be mounted" in _printed(app)


def test_a_non_interactive_boot_never_blocks_on_the_prompt(tmp_path):
    app = _app(tmp_path, FakeClient(), interactive=False)
    app.boot()
    assert app._asked == []
    assert "aiforge mount add" in _printed(app)


def test_a_live_run_blocks_the_restart_a_mount_needs(tmp_path):
    client = FakeClient(running=[3])
    client.sessions_rows.append({"id": 3, "title": "busy"})
    app = _app(tmp_path, client, answers=["y"])
    with pytest.raises(Exit) as exc:
        app.boot()
    assert exc.value.code == EXIT_ENV
    assert "attach 3" in exc.value.message


# ── a turn ─────────────────────────────────────────────────────────────────


TURN = [
    {"type": "thought", "text": "looking"},
    {"type": "tool", "name": "read_file", "args": {"path": "A"}, "result": {"lines": 2},
     "call_id": 1},
    {"type": "delta", "text": "done thinking"},
    {"type": "message", "text": "done thinking"},
    {"type": "done", "elapsed_s": 1.0},
]


def test_the_final_message_continues_the_streamed_line(tmp_path):
    # As `lines` the remainder closed the streamed row and split the answer,
    # then added a blank line between the halves.
    client = FakeClient(events=[{"type": "delta", "text": "Added "},
                                {"type": "message", "text": "Added retry."},
                                {"type": "done"}])
    app = _app(tmp_path, client, answers=["n"])
    app.boot()
    app.send("go")
    assert "Added retry." in _printed(app)
    assert "Added\nretry." not in _printed(app)


def test_a_turn_streams_and_exits_zero(tmp_path):
    client = FakeClient(events=TURN)
    app = _app(tmp_path, client, answers=["n"])
    app.boot()
    assert app.send("go") == EXIT_OK
    out = _printed(app)
    assert "▸ go" in out
    assert "read_file" in out
    assert "done thinking" in out


def test_an_agent_error_exits_one(tmp_path):
    client = FakeClient(events=[{"type": "error", "text": "model down"},
                                {"type": "done"}])
    app = _app(tmp_path, client, answers=["n"])
    app.boot()
    assert app.send("go") == EXIT_AGENT
    assert "model down" in _printed(app)


def test_json_mode_puts_nothing_but_json_on_stdout(tmp_path):
    client = FakeClient(events=[{"type": "error", "text": "x"}, {"type": "done"}])
    app = _app(tmp_path, client, answers=["n"], json_events=True)
    app.boot()
    status = app.send("go")
    stdout = app.out.getvalue()
    assert status == EXIT_AGENT
    # Every line must parse: a `jq -c .` consumer sees only events, and the
    # boot ticks, warnings and the user echo all leave by stderr.
    parsed = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    assert [e["type"] for e in parsed] == ["error", "done"]
    assert "sandbox" not in stdout
    assert "▸" not in stdout


def test_the_model_a_chat_is_pinned_to_rides_every_turn(tmp_path):
    client = FakeClient(events=TURN)
    app = _app(tmp_path, client, answers=["n"])
    app.boot()
    app.slash("/model qwen3.8-27b")
    app.send("go")
    sent = [c for c in client.calls if c[0] == "send"][-1]
    assert sent[3]["role"] == "qwen3.8-27b"


def test_an_unknown_model_is_refused_with_the_list(tmp_path):
    app = _app(tmp_path, FakeClient(), answers=["n"])
    app.boot()
    app.slash("/model nope")
    assert "no model 'nope'" in _printed(app)
    assert app.role is None


# ── approvals ──────────────────────────────────────────────────────────────


APPROVAL = [{"type": "approval", "id": 4, "tool": "file_write", "args": {"path": "x"}},
            {"type": "done"}]


def test_an_empty_answer_rejects(tmp_path):
    client = FakeClient(events=APPROVAL)
    app = _app(tmp_path, client, answers=["n", ""])
    app.boot()
    app.send("go")
    assert ("approve", 7, 4, "reject") in client.calls


def test_allow_needs_the_letter_a(tmp_path):
    client = FakeClient(events=APPROVAL)
    app = _app(tmp_path, client, answers=["n", "a"])
    app.boot()
    app.send("go")
    assert ("approve", 7, 4, "allow") in client.calls


def test_allow_all_stops_asking_for_the_rest_of_the_chat(tmp_path):
    client = FakeClient(events=[APPROVAL[0], dict(APPROVAL[0], id=5), {"type": "done"}])
    app = _app(tmp_path, client, answers=["n", "A"])
    app.boot()
    app.send("go")
    decisions = [c for c in client.calls if c[0] == "approve"]
    assert [d[3] for d in decisions] == ["allow", "allow"]
    assert len(app._asked) == 2          # the mount, then one approval


def test_with_no_terminal_an_approval_is_rejected_not_assumed(tmp_path):
    client = FakeClient(events=APPROVAL)
    app = _app(tmp_path, client, interactive=False)
    app.boot()
    app.send("go")
    assert ("approve", 7, 4, "reject") in client.calls


# ── attach, reconnect, stop ────────────────────────────────────────────────


def test_attach_adopts_the_session_it_was_asked_for(tmp_path):
    client = FakeClient(events=[{"type": "attached", "running": True},
                                {"type": "done"}])
    app = _app(tmp_path, client, answers=["n"])
    app.boot()                       # resolves chat #7 for this folder
    app.attach(12)
    # Esc during an attach must not stop the folder's own chat.
    assert app.session_id == 12
    assert ("attach", 12) in client.calls


def test_a_stream_that_keeps_dropping_gives_up_instead_of_spinning(tmp_path):
    class Dropping(FakeClient):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def send(self, session_id, content, **kw):
            raise api.Stalled("no events")

        def attach(self, session_id):
            self.attempts += 1
            raise api.Stalled("no events")

    client = Dropping()
    app = _app(tmp_path, client, answers=["n"])
    app.boot()
    assert app.send("go") == EXIT_ENV
    assert client.attempts <= 6          # bounded, with backoff
    assert "gave up re-attaching" in _printed(app)


def test_a_dead_api_mid_run_is_reported_not_raised(tmp_path):
    class Dead(FakeClient):
        def send(self, session_id, content, **kw):
            raise api.ApiDown("500 boom")

        def attach(self, session_id):
            raise api.ApiDown("500 boom")

    app = _app(tmp_path, Dead(), answers=["n"])
    app.boot()
    assert app.send("go") == EXIT_ENV


def test_slash_stop_stops_this_session(tmp_path):
    client = FakeClient()
    app = _app(tmp_path, client, answers=["n"])
    app.boot()
    app.slash("/stop")
    assert ("stop", 7) in client.calls


def test_an_unknown_slash_command_is_left_for_the_agent(tmp_path):
    app = _app(tmp_path, FakeClient(), answers=["n"])
    app.boot()
    handled, _ = app.slash("/deploy-everything")
    assert handled is False          # passed through to the backend


def test_ctx_reports_the_request_counts_the_api_actually_returns(tmp_path):
    app = _app(tmp_path, FakeClient(), answers=["n"])
    app.boot()
    app.slash("/ctx")
    out = _printed(app)
    assert "turn 3" in out
    assert "session 9" in out


def test_mount_ls_separates_approved_from_waiting(tmp_path):
    app = _app(tmp_path, FakeClient(), answers=["n"])
    work = tmp_path / "other"
    work.mkdir()
    app.cfg.mounts_file.write_text(f"{work}\n")
    lines = "\n".join(app.mount_command(["ls"]))
    assert "waiting for your approval" in lines
    assert str(work) in lines


def test_keys_esc_stops_and_typing_steers(tmp_path):
    client = FakeClient()
    app = _app(tmp_path, client, answers=["n"])
    app.boot()
    source: queue.Queue[str] = queue.Queue()
    for key in ("\x1b", "h", "i", "\r"):
        source.put(key)
    kb = KeyWatcher(source=source)
    app._keys(kb, 0, [])
    assert ("stop", 7) in client.calls
    assert ("steer", 7, "hi") in client.calls


def test_an_interrupt_key_takes_the_same_path_as_the_signal(tmp_path):
    # cbreak leaves ISIG on, so POSIX raises KeyboardInterrupt and only Windows
    # delivers \x03 as a byte. Counting it in two places gave the two
    # platforms different exit codes.
    client = FakeClient()
    app = _app(tmp_path, client, answers=["n"])
    app.boot()
    source: queue.Queue[str] = queue.Queue()
    source.put("\x03")
    kb = KeyWatcher(source=source)
    with pytest.raises(KeyboardInterrupt):
        app._keys(kb, 0, [])


def test_two_interrupts_reset_everything(tmp_path):
    class TwiceInterrupting(FakeClient):
        def __init__(self):
            super().__init__()
            self.rounds = 0

        def send(self, session_id, content, **kw):
            def gen():
                yield {"type": "thought", "text": "one"}
                raise KeyboardInterrupt
            return gen()

        def attach(self, session_id):
            self.rounds += 1

            def gen():
                yield {"type": "attached", "running": True}
                raise KeyboardInterrupt
            return gen()

    client = TwiceInterrupting()
    app = _app(tmp_path, client, answers=["n"])
    app.boot()
    assert app.send("go") == EXIT_INTERRUPT
    assert ("kill_all",) in client.calls


def test_an_attached_run_is_read_only(tmp_path):
    client = FakeClient()
    app = _app(tmp_path, client, answers=["n"])
    app.boot()
    source: queue.Queue[str] = queue.Queue()
    source.put("\x1b")
    kb = KeyWatcher(source=source)
    app._keys(kb, 0, [], True)
    assert not [c for c in client.calls if c[0] == "stop"]
    assert EXIT_INTERRUPT == 130


def test_an_interrupted_run_does_not_report_success(tmp_path):
    class Interrupting(FakeClient):
        def __init__(self):
            super().__init__()
            self.stopped = False

        def send(self, session_id, content, **kw):
            def gen():
                yield {"type": "thought", "text": "working"}
                raise KeyboardInterrupt
            return gen()

        def attach(self, session_id):
            return iter([{"type": "stopped"}])

    client = Interrupting()
    app = _app(tmp_path, client, answers=["n"])
    app.boot()
    assert app.send("go") == EXIT_INTERRUPT
    assert ("stop", 7) in client.calls


def test_an_attached_run_cannot_be_stopped_or_reset_from_the_keyboard(tmp_path):
    client = FakeClient(events=[{"type": "attached", "running": True}])
    app = _app(tmp_path, client, answers=["n"])
    app.boot()
    source: queue.Queue[str] = queue.Queue()
    source.put("\x03")
    kb = KeyWatcher(source=source)
    with pytest.raises(KeyboardInterrupt):
        app._keys(kb, 0, [], True)
    assert not [c for c in client.calls if c[0] in ("stop", "kill_all")]


def test_json_mode_still_answers_an_approval_without_dirtying_stdout(tmp_path):
    client = FakeClient(events=[{"type": "approval", "id": 2, "tool": "file_write"},
                                {"type": "done"}])
    app = _app(tmp_path, client, answers=["n", ""], json_events=True,
               interactive=False)
    app.boot()
    app.send("go")
    # Ignoring the gate in JSON mode hung the run until the server timed out;
    # answering it must not put prose in the middle of the JSON.
    assert ("approve", 7, 2, "reject") in client.calls
    for line in app.out.getvalue().splitlines():
        if line.strip():
            json.loads(line)


# ── one machine, many terminals ────────────────────────────────────────────


def test_box_down_refuses_while_another_terminal_is_running(tmp_path):
    # One machine runs ONE sandbox: `box down` is not a local action.
    client = FakeClient(running=[4])
    client.sessions_rows.append({"id": 4, "title": "someone else"})
    app = _app(tmp_path, client, answers=["n"])
    with pytest.raises(Exit) as exc:
        app.box_command(["down"])
    assert exc.value.code == EXIT_ENV
    assert "attach 4" in exc.value.message
    assert "--force" in exc.value.message


def test_box_restart_refuses_the_same_way(tmp_path):
    client = FakeClient(running=[4])
    client.sessions_rows.append({"id": 4})
    app = _app(tmp_path, client, answers=["n"])
    with pytest.raises(Exit):
        app.box_command(["restart"])


def test_force_takes_the_box_down_anyway(tmp_path, monkeypatch):
    from aiforge_cli import box as boxmod
    stopped = []
    monkeypatch.setattr(boxmod, "stop", lambda cfg: stopped.append(cfg))
    client = FakeClient(running=[4])
    client.sessions_rows.append({"id": 4})
    app = _app(tmp_path, client, answers=["n"])
    app.force = True
    assert app.box_command(["down"]) == EXIT_OK
    assert stopped, "--force should have gone through to the stop"


def test_a_second_ctrl_c_does_not_wipe_another_sessions_run(tmp_path):
    class Interrupting(FakeClient):
        def send(self, session_id, content, **kw):
            def gen():
                yield {"type": "thought", "text": "working"}
                raise KeyboardInterrupt
            return gen()

        def attach(self, session_id):
            def gen():
                yield {"type": "attached", "running": True}
                raise KeyboardInterrupt
            return gen()

    client = Interrupting(running=[4, 7])
    client.sessions_rows += [{"id": 4}, {"id": 7}]
    app = _app(tmp_path, client, answers=["n"])
    app.session_id = 7
    assert app.send("go") == EXIT_INTERRUPT
    # #4 belongs to another terminal: a twitchy second Ctrl+C must not reset it.
    assert ("kill_all",) not in client.calls
    assert ("stop", 7) in client.calls
    assert "still running on this box" in _printed(app)


def test_kill_all_asks_before_resetting_other_peoples_sessions(tmp_path):
    client = FakeClient(running=[4])
    client.sessions_rows.append({"id": 4})
    app = _app(tmp_path, client, answers=["n", "no"])
    app.boot()
    app.slash("/kill-all")
    assert ("kill_all",) not in client.calls
    assert "EVERY session" in _printed(app)


def test_kill_all_goes_ahead_when_you_confirm(tmp_path):
    client = FakeClient(running=[4])
    client.sessions_rows.append({"id": 4})
    app = _app(tmp_path, client, answers=["n", "yes"])
    app.boot()
    app.slash("/kill-all")
    assert ("kill_all",) in client.calls


def test_an_involuntary_attach_is_read_only(tmp_path):
    # Sending into a chat another terminal is already running answers 409; we
    # watch it, and Esc here must not stop THEIR run.
    class Taken(FakeClient):
        def __init__(self):
            super().__init__()
            self.first = True

        def send(self, session_id, content, **kw):
            raise api.Busy("a run is already in progress")

        def attach(self, session_id):
            self.calls.append(("attach", session_id))
            return iter([{"type": "attached", "running": True}, {"type": "done"}])

    client = Taken()
    app = _app(tmp_path, client, answers=["n"])
    app.boot()
    app.send("go")
    assert ("attach", 7) in client.calls
    assert not [c for c in client.calls if c[0] == "stop"]


# ── same repo vs different repo ────────────────────────────────────────────


class FakeGit:
    """Enough of worktrees.Git to drive App, with a record of what was asked."""

    def __init__(self, repo="/work/repo", trees=None):
        self.repo = repo
        self.added: list[str] = []
        self.removed: list[str] = []
        from aiforge_cli.worktrees import Worktree
        self.trees = trees if trees is not None else [
            Worktree(path=repo, branch="main", head="abc123")]
        self._Worktree = Worktree

    def repo_root(self, cwd):
        return self.repo if str(cwd).startswith(self.repo) else None

    def list(self, repo):
        return list(self.trees)

    def add(self, repo, name, branch=None):
        from aiforge_cli.worktrees import branch_name, worktree_path
        self.added.append(name)
        tree = self._Worktree(path=worktree_path(repo, name),
                              branch=branch or branch_name(name), head="def456")
        self.trees.append(tree)
        return tree

    def remove(self, repo, name, force=False):
        self.removed.append(name)


def test_a_different_repo_needs_no_worktree_just_another_folder(tmp_path):
    client = FakeClient()
    app = _app(tmp_path, client, answers=["n"])
    app.git = FakeGit(repo="/somewhere/else")
    lines = app.worktree_command(["ls"])
    assert "not inside a git repository" in "\n".join(lines)
    assert "run aiforge in each folder" in "\n".join(lines)


def test_worktree_add_starts_a_chat_in_the_new_tree(tmp_path):
    client = FakeClient()
    repo = tmp_path / "repo"
    (repo / ".worktrees").mkdir(parents=True)
    app = _app(tmp_path, client, answers=["n"], cwd=repo)
    app.git = FakeGit(repo=str(repo))
    app.worktree_command(["add", "fix-retry"])
    assert app.git.added == ["fix-retry"]
    # The chat is pinned to the worktree, and nothing was mounted or restarted.
    created = [c for c in client.calls if c[0] == "create"]
    assert created
    assert created[-1][1].endswith("/.worktrees/fix-retry")
    assert "wt/fix-retry" in _printed(app)


def test_worktree_add_takes_a_message_and_runs_it_there(tmp_path):
    client = FakeClient(events=[{"type": "message", "text": "done"},
                                {"type": "done"}])
    repo = tmp_path / "repo"
    repo.mkdir()
    app = _app(tmp_path, client, answers=["n"], cwd=repo)
    app.git = FakeGit(repo=str(repo))
    app.worktree_command(["add", "fix-retry", "make", "the", "test", "deterministic"])
    sent = [c for c in client.calls if c[0] == "send"]
    assert sent
    assert sent[-1][2] == "make the test deterministic"


def test_the_bare_form_is_add(tmp_path):
    client = FakeClient()
    repo = tmp_path / "repo"
    repo.mkdir()
    app = _app(tmp_path, client, answers=["n"], cwd=repo)
    app.git = FakeGit(repo=str(repo))
    app.worktree_command(["spike"])
    assert app.git.added == ["spike"]


def test_a_worktree_with_a_live_run_is_not_removed(tmp_path):
    client = FakeClient(running=[7])
    repo = tmp_path / "repo"
    repo.mkdir()
    app = _app(tmp_path, client, answers=["n"], cwd=repo)
    app.git = FakeGit(repo=str(repo))
    app.worktree_command(["add", "busy"])           # pins chat #7 to it
    client.sessions_rows.append({"id": 7})
    lines = app.worktree_command(["rm", "busy"])
    assert "is running in busy" in "\n".join(lines)
    assert app.git.removed == []


def test_a_busy_folder_offers_a_worktree_instead_of_a_second_writer(tmp_path):
    # Two agents in one checkout overwrite each other, so the answer is a
    # second working tree, not a second writer.
    client = FakeClient(running=[5])
    repo = tmp_path / "repo"
    repo.mkdir()
    app = _app(tmp_path, client, answers=["w"], cwd=repo)
    app.git = FakeGit(repo=str(repo))
    client.sessions_rows.append({"id": 5})
    from aiforge_cli import paths, sessions
    sessions.remember(app.cfg.sessions_file,
                      paths.to_box(paths.normalize_host(str(repo))), 5)
    app._resolve_session()
    assert app.git.added
    assert app.git.added[0].startswith("repo-")
    assert app.session_id != 5                      # its own chat, its own tree


def test_declining_the_offer_attaches_read_only(tmp_path):
    client = FakeClient(running=[5], events=[{"type": "attached", "running": True},
                                             {"type": "done"}])
    repo = tmp_path / "repo"
    repo.mkdir()
    app = _app(tmp_path, client, answers=["a"], cwd=repo)
    app.git = FakeGit(repo=str(repo))
    client.sessions_rows.append({"id": 5})
    from aiforge_cli import paths, sessions
    sessions.remember(app.cfg.sessions_file,
                      paths.to_box(paths.normalize_host(str(repo))), 5)
    app._resolve_session()
    assert ("attach", 5) in client.calls
    assert app.git.added == []
