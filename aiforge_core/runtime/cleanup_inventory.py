"""The cleanup inventory: what a chat created or started and has not undone.

"Clean up once everything is done" needs a list nobody has to remember:
the files the chat wrote that are still uncommitted, the background commands
and services still running (with their ids and ports), the packages it
installed, the temp paths, containers, branches, worktrees and stashes it
made, and the commits it has not pushed. Each entry says what it is, where it
is and how to undo it (the exact tool call or command when there is one).

Everything here comes from facts: :mod:`cleanup_detect` replays the chat's
tool steps, this module adds the live job table and checks each entry against
the machine (is the file still there, is the process alive, does the branch
exist). An entry that is gone is dropped. Nothing the model said is read.

The list is saved with the chat (``chat_sessions.cleanup``), so it survives a
restart of the process, a condense, a handoff restart and a rewind that
deleted the messages the entries came from.

Each step is replayed ONCE: the saved record holds how many steps it has seen.
So a step is read with the folder the chat had when it ran (a chat can move to
another folder later), and an entry that was checked and found gone stays gone
(a branch the user later creates under the same name is theirs, not ours).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time

from aiforge_core.runtime import cleanup_detect

log = logging.getLogger("aiforge.cleanup")

_VERSION = 1
MAX_ENTRIES = 60
#: Seconds one collect may spend asking git about its entries.
_VERIFY_BUDGET_S = 6.0
_ORDER = {"job": 0, "docker": 1, "file": 2, "temp": 3, "package": 4, "git": 5,
          "worktree": 6}
_PORTS = (
    re.compile(r"--port[= ](\d{2,5})\b"),
    re.compile(r"\bPORT=(\d{2,5})\b"),
    re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})\b"),
    re.compile(r"\bhttp\.server\s+(\d{2,5})\b"),
    re.compile(r"\brunserver\s+(?:[\d.]+:)?(\d{2,5})\b"),
    re.compile(r"(?:^|\s)-p\s+(\d{2,5})(?![\d:])"),
)


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_CLEANUP_INVENTORY", "1").strip().lower() \
        not in ("0", "false", "no", "off")


# ── the store ───────────────────────────────────────────────────────────────

def _load_state(session_id) -> dict:
    """What is saved with the chat: ``entries`` ({key: entry}), ``seen`` (how
    many of its tool steps were already replayed), ``mark`` (the last of them)
    and ``pending`` (commands still running when last seen). Empty when there
    is none or it cannot be read."""
    empty = {"entries": {}, "seen": 0, "mark": "", "pending": {}}
    if session_id is None:
        return empty
    try:
        from aiforge_core.runtime import chat_store
        raw = chat_store.get_session_cleanup(int(session_id))
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict) or data.get("v") != _VERSION:
            return empty
        pending = data.get("pending")
        return {"entries": {e["key"]: e for e in data.get("entries") or []
                            if isinstance(e, dict) and e.get("key")},
                "seen": max(0, int(data.get("seen") or 0)),
                "mark": str(data.get("mark") or ""),
                "pending": pending if isinstance(pending, dict) else {}}
    except Exception:  # noqa: BLE001 — a damaged record reads as none
        return empty


def load(session_id) -> dict:
    """``{key: entry}`` saved with the chat ({} when none / unreadable)."""
    return _load_state(session_id)["entries"]


_PENDING_CMD_MAX = 2000
#: Most entries of one kind kept: a chat that touched hundreds of files must not
#: push its packages, branches and stashes off the end of the list for good
#: (steps are replayed once, so what is cut here is never found again).
_PER_KIND_MAX = {"file": 30, "temp": 15}


def _pending_safe(pending: dict) -> dict:
    """The commands of jobs still running, as they are stored: masked and bounded
    (they outlive a rewind that deleted the messages they came from)."""
    out = {}
    for k, v in list((pending or {}).items())[-40:]:
        row = list(v) if isinstance(v, (list, tuple)) else [v]
        if row:
            row[0] = _safe(row[0])[:_PENDING_CMD_MAX]
        out[str(k)] = row
    return out


def _cap_kinds(entries: list) -> list:
    seen: dict = {}
    out = []
    for e in entries:
        kind = e.get("kind")
        cap = _PER_KIND_MAX.get(kind)
        if cap is not None:
            seen[kind] = seen.get(kind, 0) + 1
            if seen[kind] > cap:
                continue
        out.append(e)
    return out


def save(session_id, entries: list, *, seen: "int | None" = None,
         mark: "str | None" = None, pending: "dict | None" = None) -> bool:
    """Write the chat's list. ``seen`` / ``mark`` / ``pending`` are the replay
    position (see :func:`collect`); left out, the saved ones are kept."""
    if session_id is None:
        return False
    try:
        from aiforge_core.runtime import chat_store
        if seen is None or mark is None or pending is None:
            old = _load_state(session_id)
            seen = old["seen"] if seen is None else seen
            mark = old["mark"] if mark is None else mark
            pending = old["pending"] if pending is None else pending
        body = None
        if entries or seen or pending:
            body = json.dumps({"v": _VERSION, "entries": entries[:MAX_ENTRIES],
                               "seen": int(seen), "mark": mark,
                               "pending": _pending_safe(pending),
                               "updated_at": time.time()},
                              ensure_ascii=False, default=str)
        return bool(chat_store.set_session_cleanup(int(session_id), body))
    except Exception as exc:  # noqa: BLE001 — never break a turn over this
        log.debug("cleanup save failed (session=%s): %s", session_id, exc)
        return False


def _mark(step) -> str:
    """What identifies a step well enough to notice the list of steps changed
    under the saved position (a rewind followed by new work)."""
    if not isinstance(step, dict):
        return ""
    return f"{step.get('name')}|{step.get('args')!r}"[:200]


def session_cwd(session_id) -> str:
    """Where the chat works: its own worktree when it has one, else its folder."""
    try:
        from aiforge_core.runtime import chat_store, chat_worktree
        sess = chat_store.get_session(int(session_id)) or {}
        return str(chat_worktree.workdir_of(sess) or sess.get("cwd") or "")
    except Exception:  # noqa: BLE001
        return ""


# ── what is running ─────────────────────────────────────────────────────────

def port_of(cmd: str) -> str:
    for rx in _PORTS:
        m = rx.search(cmd or "")
        if m:
            return m.group(1)
    return ""


def _short(cmd: str, n: int = 60) -> str:
    text = cleanup_detect.redact(" ".join(str(cmd or "").split()))
    return text if len(text) <= n else text[:n - 1] + "…"


def _job_entry(key: str, cmd: str, *, pid=None, pgid=None, port: str = "",
               url: str = "", reachable: bool = True, started: str = "") -> dict:
    port = port or port_of(cmd)
    what = f"running: job {key} `{_short(cmd)}`" + (f", port {port}" if port else "")
    if reachable:
        undo = f'command_kill {{"id": "{key}"}}'
    elif pgid:
        undo = f"kill -TERM -- -{pgid}   # or press Stop in the chat"
    elif pid:
        undo = f"kill -TERM {pid}   # or press Stop in the chat"
    else:
        undo = "press Stop in the chat"
    return {"kind": "job", "key": f"job:{key}", "what": what, "where": url or "",
            "undo": undo, "job": key, "cmd": cleanup_detect.redact(str(cmd or ""))[:300],
            "pid": pid, "pgid": pgid, "port": port,
            "started": started or _proc_started(pid)}


def _alive(pid) -> bool:
    from aiforge_core.runtime import bg_work
    return bg_work._alive(pid)


def _proc_started(pid) -> str:
    """When ``pid`` started (its kernel start tick; "" when unknown). A pid is
    reused; a pid AND its start time is one process."""
    if not pid:
        return ""
    try:
        from aiforge_core.runtime.chat_turn_save import _started
        return _started(int(pid))
    except Exception:  # noqa: BLE001
        return ""


def _live_jobs(session_id) -> dict:
    """``{key: entry}`` for this chat's commands and services alive right now."""
    out: dict[str, dict] = {}
    try:
        from aiforge_core.runtime import cmd_jobs
        with cmd_jobs._LOCK:
            mine = [j for j in cmd_jobs._JOBS.values()
                    if str(j.session_id) == str(session_id)]
        services = {}
        try:
            from aiforge_core.runtime.tools import serve
            services = dict(serve._SERVICES)
        except Exception:  # noqa: BLE001
            services = {}
        for j in mine:
            if not j.alive():
                continue
            pid = getattr(j.proc, "pid", None)
            svc = services.get(pid) or {}
            e = _job_entry(j.key, j.cmd, pid=pid, pgid=getattr(j, "pgid", None),
                           port=str(svc.get("port") or ""), url=str(svc.get("url") or ""))
            out[e["key"]] = e
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.runtime import bg_work
        for row in bg_work._running_for(session_id):
            key = f"bg-{row.get('id')}"
            if f"job:{key}" in out:
                continue
            try:
                payload = json.loads(row.get("payload") or "{}")
            except Exception:  # noqa: BLE001
                payload = {}
            if row.get("kind") == "command":
                # A command the job table no longer holds (the server was
                # restarted): a row's pid alone is not proof it is the same
                # process. The saved entry, with its start time, is; see
                # ``_stored_job_still_alive``.
                continue
            out[f"job:{key}"] = {
                "kind": "job", "key": f"job:{key}",
                "what": f"running: background {row.get('kind') or 'task'} {key}"
                        + (f" `{_short(payload.get('cmd') or '')}`" if payload.get("cmd") else ""),
                "where": "", "undo": "press Stop in the chat", "job": key}
    except Exception:  # noqa: BLE001
        pass
    return out


def _stored_job_still_alive(e: dict) -> "dict | None":
    """A job saved earlier that the live table no longer holds (the process was
    restarted): kept while ITS process is alive, with the undo that still works.

    The pid alone is not proof: after a restart of the machine or the container
    the number belongs to something else, and the undo would be a ``kill`` aimed
    at it. The entry stays only while the pid has the start time that was
    saved; with no start time on record it is dropped."""
    pid = e.get("pid")
    was = str(e.get("started") or "")
    if not pid or not was or not _alive(pid) or _proc_started(pid) != was:
        return None
    return _job_entry(str(e.get("job") or f"pid-{pid}"), e.get("cmd") or "", pid=pid,
                      pgid=e.get("pgid"), port=str(e.get("port") or ""),
                      url=str(e.get("where") or ""), reachable=False, started=was)


def running_job_ids(session_id) -> "set | None":
    """The ids of this chat's jobs that are running now, or None when that
    cannot be told (so a caller does not conclude "it has ended")."""
    try:
        live = _live_jobs(session_id)
        ids = {str(e.get("job")) for e in live.values()}
        for k, e in load(session_id).items():
            if e.get("kind") == "job" and k not in live and _stored_job_still_alive(e):
                ids.add(str(e.get("job")))
        return ids
    except Exception:  # noqa: BLE001
        return None


# ── checking entries against the machine ────────────────────────────────────

def _git(args: list, cwd: str, timeout: float = 3.0) -> tuple:
    """``(returncode, stdout)``; ``(-1, "")`` when git could not be asked."""
    if not cwd or not os.path.isdir(cwd):
        return -1, ""
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return p.returncode, p.stdout or ""
    except Exception:  # noqa: BLE001
        return -1, ""


def _sandboxed() -> bool:
    """Commands run in a container: a path they made may not exist out here."""
    try:
        from aiforge_core.runtime import docker_sandbox
        return docker_sandbox.sandbox_policy() != "off"    # the env only: no probe
    except Exception:  # noqa: BLE001
        return False


def _status_map(cwd: str, rels: list) -> "dict | None":
    """``{relative path: XY}`` from ``git status`` for ``rels``; None when
    ``cwd`` is not a git checkout (or git did not answer)."""
    if not rels:
        return {}
    rc, out = _git(["-c", "core.quotepath=false", "status", "--porcelain",
                    "--untracked-files=all", "--ignored=matching", "--", *rels], cwd)
    if rc != 0:
        return None
    found: dict[str, str] = {}
    for ln in out.splitlines():
        if len(ln) < 4:
            continue
        found[ln[3:].strip().split(" -> ")[-1].strip('"')] = ln[:2]
    return found


def _code_for(rel: str, status: dict) -> str:
    if rel in status:
        return status[rel]
    for path, code in status.items():
        if path.endswith("/") and rel.startswith(path):
            return code
    return ""


#: File entries one ``git status`` is asked about (the newest ones).
_MAX_FILES = 200


def _verify_files(entries: list, cwd: str) -> list:
    """File entries that still matter: the file exists and is not committed.

    ``rm`` is offered only for a file a tool result says it CREATED. A file the
    chat changed may hold edits that were there before the chat, and one it
    wrote without that proof may have existed, so neither gets an undo: the
    entry points at ``git diff`` instead."""
    root = os.path.realpath(cwd) if cwd else ""
    inside: list = []
    kept: list = []
    for e in entries[-_MAX_FILES:]:
        path = str(e.get("path") or "")
        if not os.path.lexists(path):
            if _sandboxed():
                kept.append({**e, "verified": False})
            continue
        real = os.path.realpath(path)
        if root and (real == root or real.startswith(root + os.sep)):
            inside.append((e, os.path.relpath(real, root)))
        else:
            made = bool(e.get("created"))
            kept.append(_file_shape(e, path, "new file" if made else "file written",
                                    f"rm {shlex.quote(path)}" if made else ""))
    status = _status_map(root, [rel for _e, rel in inside]) if inside else {}
    for e, rel in inside:
        q = shlex.quote(rel)
        made = bool(e.get("created"))
        rm = f"rm {q}" if made else ""
        if status is None:                 # no git here
            kept.append(_file_shape(e, rel, "new file" if made else "file written", rm))
            continue
        code = _code_for(rel, status)
        if not code:
            continue                       # committed or reverted: nothing left
        if code == "??":
            kept.append(_file_shape(
                e, rel, "new file (untracked)" if made
                else "file written (untracked; it may predate this chat)", rm, dirty=True))
        elif code == "!!":
            kept.append(_file_shape(
                e, rel, "new file (ignored by git)" if made
                else "file written (ignored by git; it may predate this chat)", rm))
        elif "A" in code:
            kept.append(_file_shape(
                e, rel, "new file (staged, not committed)" if made
                else "file written (staged, not committed)",
                f"git rm -f --cached {q} && rm {q}" if made else "", dirty=True))
        else:
            kept.append(_file_shape(e, rel, "changed file (uncommitted)", "", dirty=True,
                                    hint=f"review with: git diff -- {q}"))
    return kept


def _file_shape(e: dict, where: str, what: str, undo: str, dirty: bool = False,
                hint: str = "") -> dict:
    return {**e, "what": what, "where": where, "undo": undo, "dirty": dirty,
            "hint": hint, "verified": True}


#: Pushing is never a cleanup step: it publishes work, and here it can deploy.
_PUSH_HINT = "not a cleanup step: push only when the user asks"


def _docker_ps() -> "list | None":
    """``[(name, id, state, compose dir, compose project)]`` for every
    container, or None when docker could not be asked."""
    import shutil
    if not shutil.which("docker"):
        return None
    fmt = ('{{.Names}}\t{{.ID}}\t{{.State}}\t'
           '{{.Label "com.docker.compose.project.working_dir"}}\t'
           '{{.Label "com.docker.compose.project"}}')
    try:
        p = subprocess.run(["docker", "ps", "-a", "--format", fmt], capture_output=True,
                           text=True, timeout=4, check=False)
    except Exception:  # noqa: BLE001
        return None
    if p.returncode != 0:
        return None
    return [tuple((ln.split("\t") + [""] * 5)[:5]) for ln in p.stdout.splitlines() if ln]


_DOCKER_CACHE: dict = {"at": 0.0, "rows": None}


def _docker_rows(fresh: bool) -> "list | None":
    """``_docker_ps()``, or its last answer when that is under 10 s old and a
    fresh one was not asked for (a status poll)."""
    now = time.monotonic()
    if not fresh and now - _DOCKER_CACHE["at"] < 10.0:
        return _DOCKER_CACHE["rows"]
    rows = _docker_ps()
    _DOCKER_CACHE.update(at=now, rows=rows)
    return rows


def _verify_docker(entries: list, fresh: bool = True) -> list:
    """Container and stack entries that still exist, with their state. With no
    docker to ask they are kept as recorded (and say so)."""
    if not entries:
        return []
    rows = _docker_rows(fresh)
    if rows is None:
        return [{**e, "verified": False, "state": ""} for e in entries]
    out = []
    for e in entries:
        key = str(e.get("key") or "")
        if key.startswith("docker:ctr:"):
            name = str(e.get("name") or key[len("docker:ctr:"):])
            # An id (hex) may be given short; a NAME must match exactly, or
            # ``db`` would adopt someone else's container whose id starts "db".
            by_id = bool(cleanup_detect._HEX.match(name))
            hit = next((r for r in rows
                        if r[0] == name or (by_id and r[1].startswith(name))), None)
        else:
            cwd, proj = str(e.get("cwd") or ""), str(e.get("project") or "")
            mine = [r for r in rows if (proj and r[4] == proj)
                    or (cwd and r[3] and os.path.realpath(r[3]) == os.path.realpath(cwd))]
            hit = next((r for r in mine if r[2] == "running"), mine[0] if mine else None)
        if hit:
            out.append({**e, "verified": True, "state": hit[2]})
    return out


def _verify_git(e: dict, worktree: str, deadline: float) -> "dict | None":
    key = str(e.get("key") or "")
    cwd = str(e.get("cwd") or "")
    if time.monotonic() > deadline:
        return {**e, "verified": False}
    if key.startswith("git:branch:"):
        rc, _ = _git(["rev-parse", "--verify", "--quiet",
                      f"refs/heads/{e.get('branch')}"], cwd)
        if rc == 1:
            return None
        if rc != 0:                        # git could not be asked: no undo
            return {**e, "undo": "", "verified": False,
                    "hint": "could not be checked; look before deleting it"}
        return {**e, "verified": True}
    if key.startswith("git:worktree:"):
        if os.path.isdir(str(e.get("path") or "")):
            return {**e, "verified": True}
        return {**e, "verified": False} if _sandboxed() else None
    if key.startswith("git:stash:"):
        # THIS stash, found by the message git printed when it was made. The
        # stash stack is shared (other chats, the user): "pop" with no ref
        # would take whichever is on top.
        rc, out = _git(["stash", "list", "--format=%gd\t%gs"], cwd)
        if rc != 0:
            return {**e, "undo": "", "verified": False}
        for row in out.splitlines():
            ref, _, msg = row.partition("\t")
            if msg.strip() == str(e.get("stash_msg") or "").strip() and ref:
                return {**e, "what": f"git stash entry {ref}",
                        "undo": f"git stash pop {shlex.quote(ref)}", "verified": True}
        return None                        # popped or dropped already
    if key.startswith("git:commits:"):
        if worktree and os.path.realpath(cwd) == os.path.realpath(worktree):
            return None                    # the chat-worktree entry covers these
        rc, out = _git(["rev-list", "--count", "@{upstream}..HEAD"], cwd)
        if rc == 0:
            n = int(out.strip() or 0) if out.strip().isdigit() else 0
            if n == 0:
                return None
            return {**e, "what": f"{n} commit{'s' if n != 1 else ''} not pushed",
                    "undo": "", "hint": _PUSH_HINT, "verified": True}
        rc2, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
        if rc2 == 0 and branch.strip():
            return {**e, "what": f"commits on `{branch.strip()}` (no upstream branch yet)",
                    "undo": "", "hint": _PUSH_HINT, "verified": True}
        return {**e, "undo": "", "hint": _PUSH_HINT, "verified": False}
    return e


def _worktree_entry(session_id) -> "tuple[dict | None, str]":
    """The chat's own worktree, when it holds work that is not merged yet."""
    try:
        from aiforge_core.runtime import chat_store, chat_worktree
        sess = chat_store.get_session(int(session_id))
        wt = chat_worktree.workdir_of(sess)
        if not wt:
            return None, ""
        data = chat_worktree.info(sess) or {}
        ahead = int(data.get("ahead") or 0)
        dirty = len(data.get("uncommitted") or [])
        if not ahead and not dirty:
            return None, str(wt)
        bits = []
        if ahead:
            bits.append(f"{ahead} commit{'s' if ahead != 1 else ''} not merged")
        if dirty:
            bits.append(f"{dirty} uncommitted file{'s' if dirty != 1 else ''}")
        return ({"kind": "worktree", "key": f"worktree:{wt}",
                 "what": f"chat worktree on branch `{data.get('branch') or '?'}`: "
                         + ", ".join(bits),
                 "where": str(wt),
                 "undo": "Merge or Discard in the chat's worktree bar",
                 "verified": True}, str(wt))
    except Exception:  # noqa: BLE001
        return None, ""


def _verify(entries: dict, session_id, cwd: str) -> list:
    deadline = time.monotonic() + _VERIFY_BUDGET_S
    wt_entry, wt = _worktree_entry(session_id)
    out: list = []
    files = [e for e in entries.values() if e.get("kind") == "file"]
    out += _verify_files(files, cwd)
    for e in entries.values():
        kind = e.get("kind")
        if kind in ("file", "job", "worktree"):
            continue
        if kind == "temp" or (kind == "package" and e.get("path")):
            if os.path.lexists(str(e.get("path") or "")):
                out.append({**e, "verified": True})
            elif _sandboxed():
                out.append({**e, "verified": False})
        elif kind == "git":
            kept = _verify_git(e, wt, deadline)
            if kept:
                out.append(kept)
        elif kind != "docker":             # packages: as recorded
            out.append({**e, "verified": False})
    out += _verify_docker([e for e in entries.values() if e.get("kind") == "docker"])
    if wt_entry:
        out.append(wt_entry)
    return out


# ── the inventory ───────────────────────────────────────────────────────────

def collect(session_id, steps: "list | None" = None, cwd: "str | None" = None,
            *, persist: bool = True) -> list:
    """The chat's cleanup entries right now, most urgent first. ``steps`` are
    ALL its tool steps (oldest first); only the ones not seen before are
    replayed. Never raises; [] when off or unknown."""
    if session_id is None or not enabled():
        return []
    try:
        cwd = cwd or session_cwd(session_id)
        state = _load_state(session_id)
        stored = state["entries"]
        steps = [s for s in (steps or []) if isinstance(s, dict) and s.get("type") == "tool"]
        seen, pending = state["seen"], state["pending"]
        if seen > len(steps) or (seen and _mark(steps[seen - 1]) != state["mark"]):
            seen, pending = 0, {}          # the steps changed under us (a rewind)
        r = cleanup_detect.Replay(cwd, jobs=pending)
        for s in steps[seen:]:
            try:
                r.step(str(s.get("name") or ""), s.get("args"), s.get("result"))
            except Exception:  # noqa: BLE001
                continue
        merged = {k: e for k, e in stored.items()
                  if e.get("kind") not in ("job", "worktree") and k not in r.resolved}
        merged.update(r.found)
        live = _live_jobs(session_id)
        jobs = dict(live)
        for k, e in stored.items():
            if e.get("kind") == "job" and k not in live:
                kept = _stored_job_still_alive(e)
                if kept:
                    jobs[kept["key"]] = kept
        entries = list(jobs.values()) + _verify(merged, session_id, cwd)
        entries = [_near(e, cwd) for e in entries]
        entries.sort(key=lambda e: _ORDER.get(e.get("kind"), 9))
        entries = _cap_kinds(entries)[:MAX_ENTRIES]
        mark = _mark(steps[-1]) if steps else ""
        if persist and (_signature(entries) != _signature(list(stored.values()))
                        or len(steps) != state["seen"] or mark != state["mark"]
                        or r.jobs != state["pending"]):
            save(session_id, entries, seen=len(steps), mark=mark, pending=r.jobs)
        return entries
    except Exception as exc:  # noqa: BLE001 — a broken inventory never breaks a turn
        log.debug("cleanup collect failed (session=%s): %s", session_id, exc)
        return []


def running(session_id) -> list:
    """Only what is RUNNING for the chat (commands, services, containers): the
    live job table plus the saved list. The steps are not replayed and the
    files are not checked, so a status poll can ask often. Never raises."""
    if session_id is None or not enabled():
        return []
    try:
        stored = load(session_id)
        live = _live_jobs(session_id)
        out = list(live.values())
        for k, e in stored.items():
            if e.get("kind") == "job" and k not in live:
                kept = _stored_job_still_alive(e)
                if kept:
                    out.append(kept)
        stacks = _verify_docker([e for e in stored.values() if e.get("kind") == "docker"],
                                fresh=False)
        return out + [e for e in stacks if e.get("state") == "running"]
    except Exception:  # noqa: BLE001
        return []


def _near(e: dict, cwd: str) -> dict:
    """``where`` as a person reads it: nothing for the chat's own folder, a
    relative path for something inside it."""
    where = str(e.get("where") or "")
    root = str(cwd or "").rstrip("/")
    if not root or not where.startswith("/"):
        return e
    if where.rstrip("/") == root:
        return {**e, "where": ""}
    if where.startswith(root + "/"):
        return {**e, "where": where[len(root) + 1:]}
    return e


def _signature(entries: list) -> str:
    return json.dumps(sorted((e.get("key"), e.get("what"), e.get("undo"))
                             for e in entries), default=str)


def _safe(text) -> str:
    """Entry text is built from commands and paths: mask credentials and drop
    anything posing as a harness marker before a person or the model reads it."""
    try:
        from aiforge_core.runtime.action_log import strip_markers
        from aiforge_core.runtime.cleanup_detect import redact
        return strip_markers(redact(str(text or "")))
    except Exception:  # noqa: BLE001
        return str(text or "")


def line(e: dict) -> str:
    """One entry as text: what, where, how to undo."""
    e = {**e, **{k: _safe(e.get(k)) for k in ("what", "where", "undo", "hint")}}
    text = str(e.get("what") or "")
    where = str(e.get("where") or "")
    if where and where not in text:
        text += f" — {where}"
    if e.get("undo"):
        text += f" → {e['undo']}"
    elif e.get("hint"):
        text += f" ({e['hint']})"
    elif e.get("kind") in ("file", "temp"):
        text += " (no automatic undo: check it before removing)"
    return text


def lines(entries: list, limit: int = 12) -> list:
    out = [t if len(t) <= 260 else t[:259] + "…" for t in map(line, entries[:limit])]
    if len(entries) > limit:
        out.append(f"(+{len(entries) - limit} more — session_actions lists them)")
    return out


def public(e: dict) -> dict:
    """An entry as the API and the tool return it."""
    return {"kind": e.get("kind"), "what": _safe(e.get("what")),
            "where": _safe(e.get("where")), "undo": _safe(e.get("undo")),
            "hint": _safe(e.get("hint")),
            "verified": bool(e.get("verified", e.get("kind") == "job"))}


def leftover_line(entries: list, *, skip_jobs=(), uncommitted: bool = True,
                  artefacts: bool = True) -> str:
    """The one line a final answer ends with: what is still running, how many
    files are uncommitted, which temp paths exist. "" when nothing is left."""
    parts = []
    jobs = [e for e in entries if e.get("kind") == "job" and e.get("job") not in skip_jobs]
    if jobs:
        names = []
        for e in jobs[:3]:
            port = f", port {e['port']}" if e.get("port") else ""
            cmd = f"`{_short(e.get('cmd') or '', 40)}`" if e.get("cmd") else "background task"
            names.append(f"job {e.get('job')} ({cmd}{port})")
        more = f" and {len(jobs) - 3} more" if len(jobs) > 3 else ""
        parts.append("Left running: " + ", ".join(names) + more + ".")
    stacks = [e for e in entries if e.get("kind") == "docker"
              and e.get("state") in ("running", "")]
    if stacks and artefacts:
        parts.append(f"Docker: {len(stacks)} container/stack"
                     f"{'s' if len(stacks) != 1 else ''} started.")
    dirty = [e for e in entries if e.get("kind") == "file" and e.get("dirty")]
    if uncommitted and dirty:
        parts.append(f"Uncommitted: {len(dirty)} file{'s' if len(dirty) != 1 else ''}.")
    temps = [e for e in entries if e.get("kind") == "temp"]
    if temps and artefacts:
        shown = ", ".join(str(e.get("where")) for e in temps[:2])
        more = f" (+{len(temps) - 2} more)" if len(temps) > 2 else ""
        parts.append(f"Temporary: {shown}{more}.")
    return " ".join(parts)


__all__ = ["MAX_ENTRIES", "collect", "enabled", "leftover_line", "line", "lines",
           "load", "port_of", "public", "running", "running_job_ids", "save",
           "session_cwd"]
