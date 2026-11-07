"""Doer agents inside a Simple-mode turn.

A big request in one chat agent is one context holding every part of it;
the user works in Simple mode for big tasks too. ``run_doers`` lets the
lead agent hand parts of its work to doer agents: each a normal chat agent
run, limited to the files it is given (``scope_globs``), with its own fresh
context and the same post-edit checks. Parts that are independent and touch
different files run side by side; a part that waits on another (``after``)
or shares a file with one runs later, in order. The lead waits for them,
gets each part's answer and the files it changed, and goes on: it merges
nothing (the doers write the same working tree), runs the tests and ends
the turn with the usual checks.

Doers never start doers, never touch the chat's goals, plan or task list
(they run without the chat session), and end before the lead's own
deadline. ``AIFORGE_DOERS=0`` turns the tool off; at most
``AIFORGE_DOERS_PARALLEL`` (default 2) run at once — a local model server
that answers one request at a time gains focus, not speed.
"""
from __future__ import annotations

import contextvars
import os
import threading
import time

_MAX_PARTS = 6
#: The lead's turn deadline (time.monotonic), set by the loop for its turn.
LEAD_DEADLINE: "contextvars.ContextVar[float | None]" = contextvars.ContextVar(
    "aiforge_lead_deadline", default=None)
#: Inside a doer: it may not start more.
IN_DOER: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "aiforge_in_doer", default=False)
#: The lead's chat session: a doer stops when that chat is stopped.
LEAD_SID: "contextvars.ContextVar[int | None]" = contextvars.ContextVar(
    "aiforge_lead_sid", default=None)
#: A doer's own deadline (seconds of wall clock), read by the turn budget.
DOER_BUDGET_S: "contextvars.ContextVar[float | None]" = contextvars.ContextVar(
    "aiforge_doer_budget_s", default=None)
#: Time the lead keeps for itself after the doers (tests, answer).
_LEAD_RESERVE_S = 240
#: All the doers of one call together, when the lead has no deadline.
_TOTAL_CAP_S = 1800
_DEFAULT_DOER_S = 900


def _on() -> bool:
    return os.environ.get("AIFORGE_DOERS", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _parallel() -> int:
    try:
        return max(1, min(_MAX_PARTS, int(os.environ.get("AIFORGE_DOERS_PARALLEL", "2"))))
    except ValueError:
        return 2


def _norm(p: str) -> str:
    """A repo-relative path: a leading ``./`` dropped (``.github``/``.env``
    keep their dot)."""
    p = str(p or "").strip()
    while p.startswith("./"):
        p = p[2:]
    return os.path.normpath(p or ".")


def _approvals_on(sid) -> bool:
    try:
        from aiforge_core.config import approval_settings
        return bool(approval_settings.required("simple", sid))
    except Exception:  # noqa: BLE001
        return False


def _dirty(cwd: str) -> set:
    """Files that differ from HEAD or are new, relative to ``cwd``."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", cwd, "status", "--porcelain", "-uall", "-z",
                            "--", "."], capture_output=True, text=True, timeout=20)
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-prefix"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return set()
    out = set()
    for row in r.stdout.split("\0"):
        path = row[3:] if len(row) > 3 else ""
        if path and "__pycache__" not in path:
            out.add(path[len(top):] if top and path.startswith(top) else path)
    return out


def _parts(args) -> "tuple[list, str]":
    """The parts as ``[{title, task, files, after}]``, or an error."""
    raw = (args or {}).get("parts") or (args or {}).get("tasks") or []
    if not isinstance(raw, list) or not raw:
        return [], "give parts: a list of {title, task, files, after?}"
    if len(raw) < 2:
        # live (attrs h1): one part handed off is the lead's own work with a
        # thinner brief, and nothing runs side by side
        return [], "one part is your own work: do it yourself (doers need 2+ independent parts)"
    if len(raw) > _MAX_PARTS:
        return [], f"at most {_MAX_PARTS} parts; do the rest yourself"
    out, titles = [], set()
    for i, p in enumerate(raw):
        if not isinstance(p, dict):
            return [], f"part {i + 1} is not an object"
        title = str(p.get("title") or f"part {i + 1}").strip()[:80]
        task = str(p.get("task") or p.get("goal") or "").strip()
        files = [f for f in (p.get("files") or []) if isinstance(f, str) and f.strip()]
        if len(task) < 20:
            return [], f"part '{title}': give the full task text (what to build, names, rules)"
        if not files:
            return [], (f"part '{title}': list the files it may change (a doer only "
                        "writes those)")
        if any(_norm(f).startswith("..") or os.path.isabs(_norm(f)) for f in files):
            return [], f"part '{title}': files must be paths inside the repository"
        if title in titles:
            title = f"{title} ({i + 1})"
        titles.add(title)
        after = [str(a) for a in (p.get("after") or []) if str(a).strip()]
        out.append({"title": title, "task": task, "files": [_norm(f) for f in files],
                    "after": after})
    known = {p["title"] for p in out}
    for p in out:
        bad = [a for a in p["after"] if a not in known]
        if bad:
            return [], f"part '{p['title']}': after names no part: {', '.join(bad)}"
    return out, ""


def waves(parts: list) -> "list[list]":
    """Parts in order of running: a part runs after the parts it names in
    ``after`` and after any earlier part sharing one of its files; parts in
    one wave are independent and file-disjoint."""
    done, left, out = set(), list(parts), []
    while left:
        wave, files = [], set()
        for p in list(left):
            earlier = [q for q in parts[:parts.index(p)] if q["title"] not in done]
            waits = set(p["after"]) - done
            shares = any(set(p["files"]) & set(q["files"]) for q in earlier if q not in wave)
            if waits or shares or set(p["files"]) & files:
                continue
            wave.append(p)
            files |= set(p["files"])
        if not wave:                       # a cycle in after: run the rest in order
            wave = [left[0]]
        for p in wave:
            left.remove(p)
            done.add(p["title"])
        out.append(wave)
    return out


def _brief(part: dict, others: list) -> str:
    rest = "; ".join(f"{o['title']} ({', '.join(o['files'])})" for o in others
                     if o is not part)
    return (f"You are a doer on ONE part of a larger request. Your part: {part['title']}.\n\n"
            f"{part['task']}\n\n"
            f"Change ONLY these files: {', '.join(part['files'])}. "
            + (f"Other doers are working on other parts at the same time: {rest}. "
               "Do not touch their files. " if rest else "")
            + "Read what you need, make the change, run the tests that cover your "
            "part, and fix what fails. Then reply FINAL: what you changed (files, "
            "names) and what you checked.")


def _budget_s() -> float:
    lead = LEAD_DEADLINE.get()
    cap = _DEFAULT_DOER_S
    try:
        cap = float(os.environ.get("AIFORGE_DOER_DEADLINE_S", cap))
    except ValueError:
        pass
    if lead is None:
        return cap
    return max(0.0, min(cap, lead - time.monotonic() - _LEAD_RESERVE_S))


def _snapshot(cwd: str, files: list) -> dict:
    """``{file: content digest or None}`` for a part's files (a folder's files
    one level down and deeper), relative to ``cwd``."""
    import hashlib
    out = {}
    for f in files:
        full = os.path.join(cwd, f)
        paths = [full]
        if os.path.isdir(full):
            paths = [os.path.join(d, n) for d, _s, ns in os.walk(full)
                     if "__pycache__" not in d for n in ns]
        for p in paths:
            rel = os.path.relpath(p, cwd)
            try:
                with open(p, "rb") as fh:
                    out[rel] = hashlib.sha1(fh.read()).hexdigest()
            except OSError:
                out[rel] = None
    return out


def _changed_in(cwd: str, files: list) -> list:
    """Files of ``files`` (paths or folders) that differ from HEAD or are new,
    from git — the doers share one tree, so each reports only its own."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", cwd, "status", "--porcelain", "-uall", "-z"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for row in r.stdout.split("\0"):
        path = row[3:] if len(row) > 3 else ""
        if not path or "__pycache__" in path:
            continue
        if any(path == f or path.startswith(f.rstrip("/") + "/") for f in files):
            out.append(path)
    return sorted(set(out))


def _run_one(part: dict, others: list, cwd: str, budget: float, lead_sid=None) -> dict:
    from aiforge_core.runtime.chat_agent import run_chat_agent
    final, steps, err = "", 0, ""
    LEAD_SID.set(lead_sid)                 # this doer's own context only
    before = _snapshot(cwd, part["files"])
    IN_DOER.set(True)
    DOER_BUDGET_S.set(budget)
    # its files, as written and absolute (the scope check sees either form)
    scope = list(dict.fromkeys(list(part["files"]) + [
        os.path.join(os.path.realpath(cwd), f) for f in part["files"]]))
    t0 = time.monotonic()
    try:
        for ev in run_chat_agent([{"role": "user", "content": _brief(part, others)}],
                                 cwd=cwd, role="chat", session_id=None,
                                 scope_globs=scope, strict_finish=True):
            kind = ev.get("type")
            if kind == "tool":
                steps += 1
            elif kind == "message" and not ev.get("supplementary"):
                final = ev.get("text") or final
            elif kind == "error":
                err = str(ev.get("text") or "")[:300]
    except Exception as exc:  # noqa: BLE001 — one doer's crash is its own result
        err = str(exc)[:300]
    after = _snapshot(cwd, part["files"])
    changed = sorted(f for f in set(before) | set(after) if before.get(f) != after.get(f))
    return {"title": part["title"], "ok": bool(final) and not err,
            "files_changed": changed, "steps": steps,
            "secs": round(time.monotonic() - t0),
            "answer": (final or "")[-1500:], **({"error": err} if err else {})}


def _t_run_doers(args: dict, cwd: str) -> dict:
    if not _on():
        return {"ok": False, "error": "doers are off: do the work yourself"}
    if IN_DOER.get():
        return {"ok": False, "error": "a doer does its own part: do it yourself"}
    raw_parts = (args or {}).get("parts") or (args or {}).get("tasks") or []
    real = os.path.realpath(cwd) + os.sep
    for p in raw_parts if isinstance(raw_parts, list) else []:
        if isinstance(p, dict) and isinstance(p.get("files"), list):
            p["files"] = [os.path.relpath(os.path.realpath(f), real.rstrip(os.sep))
                          if isinstance(f, str) and os.path.isabs(f)
                          and os.path.realpath(f).startswith(real) else f
                          for f in p["files"]]
    parts, why = _parts(args)
    if why:
        return {"ok": False, "error": why}
    budget = _budget_s()
    if budget < 120:
        return {"ok": False, "error": "not enough turn time left for doers: do it yourself"}
    try:
        from aiforge_core.runtime import chat_cancel, request_context
        raw = request_context.get_session_id()
        lead_sid = int(raw) if raw is not None else None
    except Exception:  # noqa: BLE001
        chat_cancel, lead_sid = None, None
    if lead_sid is not None and _approvals_on(lead_sid):
        # doers run without the chat, so nothing would ask: keep the user's choice
        return {"ok": False, "error": "approvals are on in this chat: do the work "
                "yourself so each change can be approved"}
    dirty0 = _dirty(cwd)
    t_start = time.monotonic()

    def stopped() -> bool:
        try:
            return bool(lead_sid is not None and chat_cancel.is_cancelled(lead_sid))
        except Exception:  # noqa: BLE001
            return False
    results: dict = {}
    order = waves(parts)
    for wave in order:
        lock = threading.Semaphore(_parallel())
        threads = []

        def run(p):
            with lock:
                left = _budget_s() if LEAD_DEADLINE.get() is not None else \
                    min(budget, _TOTAL_CAP_S - (time.monotonic() - t_start))
                if left < 60 or stopped():
                    results[p["title"]] = {"title": p["title"], "ok": False, "files_changed": [],
                                           "steps": 0, "secs": 0, "answer": "",
                                           "error": "not run: no turn time left (or stopped) — "
                                                    "do this part yourself"}
                    return
                results[p["title"]] = _run_one(p, parts, cwd, left, lead_sid)

        for p in wave:
            ctx = contextvars.copy_context()
            t = threading.Thread(target=ctx.run, args=(run, p), daemon=True,
                                 name=f"doer-{p['title'][:20]}")
            t.start()
            threads.append(t)
        for t in threads:
            while t.is_alive():
                t.join(1.0)
                if stopped():
                    break                  # the doers stop at their next step
        if stopped():
            break
    out = [results[p["title"]] for p in parts if p["title"] in results]
    allowed = {f for p in parts for f in p["files"]}
    stray = sorted(f for f in _dirty(cwd) - dirty0
                   if not any(f == a or f.startswith(a.rstrip("/") + "/") for a in allowed))
    return {"ok": all(r["ok"] for r in out),
            **({"touched_outside_their_files": stray} if stray else {}),
            "waves": [[p["title"] for p in w] for w in order],
            "results": out,
            "note": ("The doers wrote to this working tree. Check their answers, run "
                     "the whole test suite, and fix what is still wrong yourself.")}


__all__ = ["DOER_BUDGET_S", "IN_DOER", "LEAD_DEADLINE", "LEAD_SID", "waves", "_t_run_doers"]
