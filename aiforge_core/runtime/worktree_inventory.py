"""AIForge's OLD worktrees of a project: list them, and remove only what the
user picks.

AIForge leaves worktrees behind: a chat's (``chat-<id>-<tok>``), a ticket's
(``<KEY>-<n>``), a branch folder (``branch-<name>``), and folders git no longer
knows at all. Nothing removed them unless a janitor ran, and a janitor cannot
know which of them still hold work worth keeping. This module only LISTS them
(:func:`old_entries`) and removes exactly the entries the user named
(:func:`delete`); every branch is kept:

* a worktree git still registers: uncommitted changes are committed to its own
  branch first (a detached one gets a rescue branch), then
  ``git worktree remove --force`` + ``git worktree prune``;
* a dead folder (git no longer registers it) is MOVED to
  ``<config>/worktree-trash/<stamp>/<name>``, never deleted; trash older than
  :data:`TRASH_DAYS` days is pruned when this code runs.

ACTIVE entries (a chat that still points at it, a ticket still being worked, a
branch folder used in the last :data:`OTHER_IDLE_DAYS` days, anything touched in
the last few minutes) are never listed and never touched.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

log = logging.getLogger(__name__)

_IN_REPO = ".aiforge-worktrees"
TERMINAL = frozenset({"done", "cancelled", "qa_failed"})
TRASH_DAYS = 14
KEEP_DAYS = 7
#: A folder touched this recently may be in the middle of being made.
_MIN_AGE_S = 600
#: A branch folder or other worktree without an owner we can ask is old only
#: after this long without a change.
OTHER_IDLE_DAYS = 14
_SIZE_FILES_CAP = 200_000

_CHAT = re.compile(r"^chat-(\d+)-[0-9a-z]+$", re.I)
_TICKET = re.compile(r"^(?!chat-|branch-)([A-Za-z][A-Za-z0-9_]*-\d+)$")
_STATE_LOCK = threading.Lock()


def _git(args, cwd: str, timeout: float = 60) -> "subprocess.CompletedProcess | None":
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        log.debug("git %s failed in %s: %s", args[:2], cwd, exc)
        return None


def _out(args, cwd: str, timeout: float = 60) -> str:
    p = _git(args, cwd, timeout)
    return (p.stdout or "").strip() if p is not None and p.returncode == 0 else ""


def _cfg() -> str:
    from aiforge_core.config.paths import config_dir
    return str(config_dir())


def trash_root() -> str:
    return os.path.join(_cfg(), "worktree-trash")


def _real(p: str) -> str:
    return os.path.realpath(p)


# ── what is there ────────────────────────────────────────────────────────────

def _registered(repo: str) -> dict:
    """realpath -> {"branch": short name or "", "head": sha} for every linked
    worktree git knows (the main checkout left out)."""
    out = _out(["worktree", "list", "--porcelain"], repo)
    rows: dict = {}
    cur: "dict | None" = None
    for ln in out.splitlines() + [""]:
        if ln.startswith("worktree "):
            cur = {"path": ln[len("worktree "):].strip(), "branch": "", "head": ""}
        elif cur is not None and ln.startswith("HEAD "):
            cur["head"] = ln[5:].strip()
        elif cur is not None and ln.startswith("branch "):
            cur["branch"] = ln[7:].strip().removeprefix("refs/heads/")
        elif not ln.strip() and cur is not None:
            rows[_real(cur["path"])] = cur
            cur = None
    rows.pop(_real(repo), None)
    return rows


def _holders(repo: str) -> list[tuple[str, str]]:
    """(folder, where) that hold this repo's AIForge worktrees."""
    out = [(os.path.join(repo, _IN_REPO), "repo")]
    try:
        from aiforge_core.runtime import chat_worktree
        out.append((chat_worktree.config_holder(repo), "config"))
    except Exception:  # noqa: BLE001
        pass
    return out


def remove_empty_holder(repo: str) -> bool:
    """Drop ``<repo>/.aiforge-worktrees`` when it is an empty folder."""
    holder = os.path.join(repo, _IN_REPO)
    try:
        if os.path.isdir(holder) and not os.path.islink(holder) and not os.listdir(holder):
            os.rmdir(holder)
            return True
    except OSError:
        pass
    return False


def _age_s(entry: str, wt: "str | None") -> float:
    paths = [entry]
    try:
        paths += [os.path.join(entry, n) for n in os.listdir(entry)]
    except OSError:
        pass
    if wt and wt != entry:
        paths.append(wt)
    gd = _gitdir(wt) if wt else None
    if gd:
        paths += [os.path.join(gd, "index"), os.path.join(gd, "HEAD"),
                  os.path.join(gd, "logs", "HEAD")]
    newest = 0.0
    for p in paths:
        try:
            newest = max(newest, os.lstat(p).st_mtime)
        except OSError:
            continue
    return max(0.0, time.time() - newest) if newest else 0.0


def _gitdir(wt: "str | None") -> "str | None":
    try:
        with open(os.path.join(str(wt), ".git"), encoding="utf-8") as fh:
            first = fh.readline().strip()
        if first.startswith("gitdir:"):
            gd = first[len("gitdir:"):].strip()
            return gd if os.path.isdir(gd) else None
    except OSError:
        pass
    return None


def _size(path: str) -> tuple[int, bool]:
    """Bytes under ``path`` (links not followed) and whether the count was cut."""
    total, files = 0, 0
    for root, dirs, names in os.walk(path, followlinks=False):
        for n in names:
            try:
                total += os.lstat(os.path.join(root, n)).st_size
            except OSError:
                continue
            files += 1
            if files >= _SIZE_FILES_CAP:
                return total, True
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
    return total, False


def _meta(entry: str) -> dict:
    try:
        with open(os.path.join(entry, "meta.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _chat_owner(sid: int, entry: str) -> tuple[str, str]:
    try:
        from aiforge_core.runtime import chat_store
        sess = chat_store.get_session(sid)
    except Exception as exc:  # noqa: BLE001 — unknown owner is not proof it is gone
        log.debug("session lookup failed: %s", exc)
        return "active", "chat state unreadable"
    if not sess:
        return "gone", "chat deleted"
    wd = sess.get("workdir") or ""
    if wd:
        r, e = _real(wd), _real(entry)
        if r == e or r.startswith(e + os.sep):
            return "active", "open chat"
    return "moved", f"chat {sid} no longer uses it"


def _ticket_owner(key: str) -> tuple[str, str]:
    try:
        from aiforge_core.tickets import store
        t = store.get(key)
        status = getattr(t, "status", None) if t else None
    except Exception as exc:  # noqa: BLE001 — a store hiccup keeps the tree
        log.debug("ticket lookup failed for %s: %s", key, exc)
        return "active", "ticket store unreadable"
    if not status:
        return "unknown", "ticket unknown"
    status = str(getattr(status, "value", status))
    if status in TERMINAL:
        return "terminal", f"ticket {status}"
    return "active", f"ticket {status}"


def _unmerged(repo: str, base: str, tip: str) -> "int | None":
    if not tip:
        return None
    if not base or not _out(["rev-parse", "--verify", "-q", base], repo):
        base = "HEAD"
    n = _out(["rev-list", "--count", f"{base}..{tip}"], repo)
    return int(n) if n.isdigit() else None


def inventory(repo: str, *, with_size: bool = True) -> list[dict]:
    """Every AIForge worktree folder of ``repo`` with what it holds and who owns
    it. See the module doc for ``old``."""
    repo = _real(repo)
    remove_empty_holder(repo)
    reg = _registered(repo)
    base_default = _out(["symbolic-ref", "--short", "-q", "HEAD"], repo)
    out: list[dict] = []
    for holder, where in _holders(repo):
        try:
            names = sorted(os.listdir(holder))
        except OSError:
            continue
        for name in names:
            entry = os.path.join(holder, name)
            if not os.path.isdir(entry) or os.path.islink(entry):
                continue
            try:
                out.append(_describe(repo, entry, name, where, reg, base_default, with_size))
            except Exception as exc:  # noqa: BLE001 — one odd folder never hides the rest
                log.debug("worktree %s not described: %s", entry, exc)
    return out


def _describe(repo, entry, name, where, reg, base_default, with_size) -> dict:
    work = os.path.join(entry, "work")
    wt = None
    for cand in (work, entry):
        if _real(cand) in reg:
            wt = cand
            break
    registered = wt is not None
    shown_wt = wt or (work if os.path.isdir(work) else entry)
    m_chat, m_ticket = _CHAT.match(name), _TICKET.match(name)
    if m_chat:
        kind = "chat"
        owner, detail = _chat_owner(int(m_chat.group(1)), entry)
    elif m_ticket:
        kind = "ticket"
        owner, detail = _ticket_owner(m_ticket.group(1))
    else:
        kind, owner, detail = "other", "idle", "no owner on record"
    meta = _meta(entry)
    branch, tip = "", ""
    if registered:
        row = reg[_real(wt)]
        branch, tip = row.get("branch") or "", row.get("head") or ""
    elif meta.get("branch") and _out(["rev-parse", "--verify", "-q",
                                      f"refs/heads/{meta['branch']}"], repo):
        branch = tip = meta["branch"]
    # Age first: it reads the index's mtime, and looking must not count as use.
    age = _age_s(entry, wt)
    uncommitted = 0
    if registered and os.path.isdir(wt):
        st = _out(["--no-optional-locks", "status", "--porcelain"], wt)
        uncommitted = len([ln for ln in st.splitlines() if ln.strip()])
    base = meta.get("base_branch") or base_default
    unmerged = _unmerged(repo, base, tip)
    size, cut = _size(entry) if with_size else (0, False)
    if age < _MIN_AGE_S or owner == "active":
        old = False                  # in use, or in the middle of being made
    elif not registered:
        old = True                   # a dead folder is never anyone's
    elif kind == "chat":
        old = owner in ("gone", "moved")
    elif kind == "ticket":
        old = owner in ("terminal", "unknown")
    else:
        old = age >= OTHER_IDLE_DAYS * 86400
    return {"name": name, "path": entry, "worktree": shown_wt, "where": where,
            "kind": kind, "owner": owner, "owner_detail": detail,
            "registered": registered, "dead": not registered,
            "uncommitted": uncommitted, "unmerged": unmerged, "branch": branch,
            "age_days": round(age / 86400, 1), "size": size, "size_cut": cut,
            "old": old}


def old_entries(repo: str) -> list[dict]:
    return [e for e in inventory(repo) if e["old"]]


def set_key(entries) -> str:
    paths = sorted(_real(e["path"]) for e in entries)
    return hashlib.sha1("\n".join(paths).encode("utf-8"),
                        usedforsecurity=False).hexdigest()[:12]


# ── showing them ─────────────────────────────────────────────────────────────

def _mb(n: int, cut: bool = False) -> str:
    s = f"{n / 1e6:.1f} MB" if n >= 100_000 else f"{max(1, -(-max(n, 0) // 1000))} KB"
    return (">" + s) if cut else s


def unsaved(e: dict) -> bool:
    return bool(e.get("uncommitted")) or (e.get("dead") and e.get("unmerged") is None)


def format_list(entries, trash: "str | None" = None) -> str:
    trash = trash or trash_root()
    lines = [f"Old AIForge worktrees in this project ({len(entries)}; no open chat or "
             "active ticket uses them):"]
    for i, e in enumerate(entries, 1):
        bits = [e["kind"] + (f" ({e['owner_detail']})" if e.get("owner_detail") else "")]
        if e["dead"]:
            bits.append("dead folder (git no longer registers it)")
        bits.append(f"{e['age_days']:g} days old")
        if e["uncommitted"]:
            bits.append(f"{e['uncommitted']} uncommitted")
        un = e.get("unmerged")
        bits.append("unmerged commits: ?" if un is None else f"{un} unmerged commit{'s' if un != 1 else ''}")
        bits.append(_mb(e["size"], e.get("size_cut")))
        flag = "  ⚠ unsaved work" if unsaved(e) else ""
        lines.append(f"{i}. {e['name']} — " + " · ".join(bits) + flag)
        lines.append(f"   {e['path']}")
    lines.append('Reply "delete worktrees 1,3", "delete worktrees all" or "keep worktrees". '
                 "Every branch is kept: uncommitted changes are committed to the worktree's "
                 f"own branch first, and a dead folder is moved to {trash}{os.sep} "
                 "(recoverable), not deleted.")
    if any(unsaved(e) for e in entries):
        lines.append("⚠ marks entries with work that is not on any branch yet "
                     "(\"all\" includes them: it is committed or moved to the trash, not lost).")
    return "\n".join(lines)


_DELETE = re.compile(r"\b(?:delete|remove|drop)\s+(?:the\s+)?(?:old\s+)?worktrees?\s*"
                     r"(?P<what>all|(?:#?\d+(?:\s*(?:,|and|&|\s)\s*#?\d+)*))", re.I)
_KEEP = re.compile(r"\bkeep\s+(?:the\s+|all\s+|them\s+all\s+)?(?:old\s+)?worktrees?\b", re.I)


def parse_command(text: "str | None"):
    """``(("delete", "all" | [1-based numbers]) | ("keep", None) | None, rest)``,
    ``rest`` being the message with the command taken out."""
    t = text or ""
    m = _DELETE.search(t)
    if m:
        what = m.group("what").strip().lower()
        picks = "all" if what == "all" else sorted({int(n) for n in re.findall(r"\d+", what)})
        rest = (t[:m.start()] + " " + t[m.end():]).strip(" ,.;")
        return ("delete", picks), rest
    m = _KEEP.search(t)
    if m:
        rest = (t[:m.start()] + " " + t[m.end():]).strip(" ,.;")
        return ("keep", None), rest
    return None, t


# ── removing the chosen ones ─────────────────────────────────────────────────

def _git_env() -> dict:
    env = dict(os.environ)
    for k, v in (("GIT_AUTHOR_NAME", "aiforge"), ("GIT_COMMITTER_NAME", "aiforge"),
                 ("GIT_AUTHOR_EMAIL", "aiforge@local"), ("GIT_COMMITTER_EMAIL", "aiforge@local")):
        env.setdefault(k, v)
    return env


def _commit_all(wt: str, name: str) -> int:
    st = _out(["status", "--porcelain"], wt)
    n = len([ln for ln in st.splitlines() if ln.strip()])
    if not n:
        return 0
    try:
        subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True, timeout=120)
        p = subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify",
                            "-m", f"aiforge: saved before removing worktree {name}"],
                           cwd=wt, capture_output=True, text=True, timeout=120, env=_git_env())
        return n if p.returncode == 0 else -1
    except Exception as exc:  # noqa: BLE001
        log.warning("commit before removing %s failed: %s", wt, exc)
        return -1


def _move_to_trash(path: str, stamp: str) -> str:
    dest_dir = os.path.join(trash_root(), stamp)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(path))
    if os.path.exists(dest):
        dest = f"{dest}-{uuid.uuid4().hex[:4]}"
    shutil.move(path, dest)
    return dest


def prune_trash(days: int = TRASH_DAYS) -> int:
    root = trash_root()
    cut = time.time() - days * 86400
    n = 0
    try:
        for name in os.listdir(root):
            p = os.path.join(root, name)
            try:
                if os.path.isdir(p) and os.lstat(p).st_mtime < cut:
                    shutil.rmtree(p, ignore_errors=True)
                    n += 1
            except OSError:
                continue
    except OSError:
        pass
    return n


def _remove_one(repo: str, e: dict, stamp: str) -> str:
    name = e["name"]
    if e["registered"] and os.path.isdir(e["worktree"]):
        wt = e["worktree"]
        saved = _commit_all(wt, name)
        if saved < 0:
            return f"{name}: kept — its uncommitted changes could not be committed"
        branch = e.get("branch") or ""
        if not branch:
            # Detached: a branch keeps its commits reachable.
            branch = f"aiforge/rescued-{name}-{stamp}"
            p = _git(["branch", branch, "HEAD"], wt)
            if p is None or p.returncode != 0:
                return f"{name}: kept — could not put its commits on a branch"
        p = _git(["worktree", "remove", "--force", wt], repo, 120)
        if p is None or p.returncode != 0:
            err = ((p.stderr or "").strip().splitlines()[-1:] if p is not None else [""])[0]
            return f"{name}: kept — git refused to remove it ({err[:120]})"
        _git(["worktree", "prune"], repo, 30)
        extra = ""
        if os.path.isdir(e["path"]):
            try:
                if os.listdir(e["path"]):
                    extra = f"; its other files went to {_move_to_trash(e['path'], stamp)}"
                else:
                    os.rmdir(e["path"])
            except OSError:
                pass
        what = f", {saved} uncommitted file{'s' if saved != 1 else ''} committed first" if saved else ""
        return f"{name}: removed (branch `{branch}` kept{what}){extra}"
    dest = _move_to_trash(e["path"], stamp)
    _git(["worktree", "prune"], repo, 30)
    return f"{name}: dead folder moved to {dest}"


def delete(repo: str, listed: list[str], picks) -> list[str]:
    """Remove the ``picks`` (1-based numbers into ``listed``, the paths the user
    was shown, or ``"all"``). An entry that is no longer old (a chat picked it
    up again) is skipped. Returns one line per pick."""
    repo = _real(repo)
    if picks == "all":
        idx = list(range(1, len(listed) + 1))
    else:
        idx = [i for i in picks if 1 <= i <= len(listed)]
    lines = [f"#{i}: no such entry in the list" for i in (picks if picks != "all" else [])
             if not 1 <= i <= len(listed)]
    now = {_real(e["path"]): e for e in inventory(repo, with_size=False)}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for i in idx:
        path = _real(listed[i - 1])
        e = now.get(path)
        if e is None:
            lines.append(f"{os.path.basename(path)}: already gone")
            continue
        if not e["old"]:
            lines.append(f"{e['name']}: kept — it is in use again")
            continue
        try:
            lines.append(_remove_one(repo, e, stamp))
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{e['name']}: kept — {str(exc)[:160]}")
    remove_empty_holder(repo)
    try:
        prune_trash()
    except Exception:  # noqa: BLE001
        pass
    return lines


# ── "keep": not again for a while ────────────────────────────────────────────

def _state_path() -> str:
    return os.path.join(_cfg(), "worktree_cleanup_state.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def snooze(repo: str, key: str, days: int = KEEP_DAYS) -> None:
    with _STATE_LOCK:
        data = _load_state()
        data[_real(repo)] = {"key": key, "until": time.time() + days * 86400}
        try:
            os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
            tmp = f"{_state_path()}.{uuid.uuid4().hex[:8]}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, _state_path())
        except OSError as exc:
            log.warning("keep-worktrees state not written: %s", exc)


def snoozed(repo: str, key: str) -> bool:
    row = _load_state().get(_real(repo))
    return isinstance(row, dict) and row.get("key") == key \
        and float(row.get("until") or 0) > time.time()


__all__ = ["inventory", "old_entries", "set_key", "format_list", "parse_command",
           "delete", "prune_trash", "snooze", "snoozed", "trash_root",
           "remove_empty_holder", "unsaved"]
