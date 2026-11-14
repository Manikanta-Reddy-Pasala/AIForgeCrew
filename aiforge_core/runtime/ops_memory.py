"""Operational memory: how to reach each host and how to run code in each repo.

Free-text memory notes were the only place this lived, and it did not hold:
- "plain ssh fails on that box, use paramiko" was kept only if the end-of-turn
  learner model happened to write it down;
- per-host access (an ssh key for one box, a password for another, chosen by
  name or IP) had no record at all, so with several hosts the notes conflicted
  and the model mixed them up;
- "run Python with this interpreter from this folder" was saved as up to three
  raw commands a turn, one-off probes with temp paths included.

So two small structured stores, written without a model call:

``hosts``  one entry per machine: name, aliases, addresses (IP / DNS), user,
           port, method (``ssh_key`` | ``password`` | ``paramiko`` | ``other``),
           key path, ``secret`` (the NAME of a secret saved with save_secret —
           never a value), ``recipe`` (the command shape that worked) and
           ``avoid`` (what failed, e.g. "plain ssh: password prompt").
``repos``  one entry per repo key: python interpreter, working folder, env
           (PYTHONPATH...), test command, run command.
``fixes``  one entry per failure fingerprint (``failure_signature``: no line
           numbers, temp paths or timings): the files and functions whose
           change turned that failure green. Saved only with that proof (the
           same run failed, then went green after edits); shown only when the
           same failure comes back, on that test result — never in the prompt.

Repo locations are learned too, into the existing ``repos.json``
(``config.repo_map``): the folder a chat worked in, for a repo that has none.

Each field remembers who set it: what the USER said is never overwritten by
something learned from a command. Lookup is deterministic: a host is shown to
the model only when the request names it (name, alias or address), a repo's
recipe only in that repo. ``AIFORGE_OPS_MEMORY=0`` turns it off.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

_LOCK = threading.Lock()
_FILE = "ops_memory.json"
METHODS = ("ssh_key", "password", "paramiko", "other")
HOST_FIELDS = ("aliases", "addresses", "user", "port", "method", "key_path",
               "secret", "recipe", "avoid", "notes")
REPO_FIELDS = ("python", "cwd", "env", "test_cmd", "run_cmd")
_MAX_AVOID = 4


def enabled() -> bool:
    return os.environ.get("AIFORGE_OPS_MEMORY", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _path() -> str:
    from aiforge_core.config.paths import config_dir
    return os.path.join(str(config_dir()), _FILE)


def load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("hosts", {})
    data.setdefault("repos", {})
    data.setdefault("fixes", {})
    return data


def _save(data: dict) -> None:
    from aiforge_core.config import _atomic
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic.write_text(path, json.dumps(data, indent=2, sort_keys=True))


def repo_root(cwd) -> str:
    """The git top level of ``cwd`` (paths in a recipe are kept relative to it)."""
    import subprocess
    try:
        p = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return str(cwd or "")
    return p.stdout.strip() if p.returncode == 0 else str(cwd or "")


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", str(name or "").strip().lower()).strip("-")


def _merge(entry: dict, fields: dict, source: str, allowed) -> bool:
    """Write ``fields`` into ``entry``; a field the user set is not replaced by
    a learned value. Returns whether anything changed."""
    src = entry.setdefault("_source", {})
    changed = False
    for k, v in fields.items():
        if k not in allowed or v in (None, "", [], {}):
            continue
        if source != "user" and src.get(k) == "user":
            continue
        if k in ("aliases", "addresses", "avoid"):
            old = list(entry.get(k) or [])
            new = old + [x for x in (v if isinstance(v, list) else [v]) if x not in old]
            if k == "avoid":
                new = new[-_MAX_AVOID:]
            if new != old:
                entry[k] = new
                changed = True
        elif entry.get(k) != v:
            entry[k] = v
            changed = True
        if source == "user":
            src[k] = "user"
        else:
            src.setdefault(k, "learned")
    if changed:
        entry["updated"] = time.strftime("%Y-%m-%d")
    return changed


# ── hosts ─────────────────────────────────────────────────────────────────
def upsert_host(name: str, fields: dict, *, source: str = "learned") -> dict:
    """Create or update a host. ``source`` is ``user`` (what the user said) or
    ``learned`` (seen working in a command)."""
    k = _key(name)
    if not k:
        return {"ok": False, "error": "host name required"}
    if fields.get("method") and fields["method"] not in METHODS:
        return {"ok": False, "error": f"method must be one of {', '.join(METHODS)}"}
    if "secret" in fields and fields["secret"] and not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,64}", str(fields["secret"])):
        return {"ok": False, "error": "secret: the NAME of a saved secret, not a value"}
    with _LOCK:
        data = load()
        entry = data["hosts"].get(k) or {"name": name}
        changed = _merge(entry, fields, source, HOST_FIELDS)
        data["hosts"][k] = entry
        if changed:
            _save(data)
    return {"ok": True, "host": k, "changed": changed}


def _alias_re(word: str) -> "re.Pattern | None":
    word = str(word or "").strip()
    if len(word) < 2:
        return None
    return re.compile(r"(?<![\w.-])" + re.escape(word) + r"(?![\w-])", re.I)


def find_hosts(text: str) -> list[dict]:
    """Hosts the text names: by name, alias or address (whole word)."""
    if not text:
        return []
    out = []
    for k, h in load()["hosts"].items():
        words = [h.get("name") or k, k, *(h.get("aliases") or []), *(h.get("addresses") or [])]
        if any((rx := _alias_re(w)) and rx.search(text) for w in words):
            out.append(h)
    return out


def host_for_address(addr: str) -> "str | None":
    addr = str(addr or "").strip().lower()
    for k, h in load()["hosts"].items():
        names = [k, str(h.get("name") or "").lower(),
                 *(str(a).lower() for a in h.get("aliases") or []),
                 *(str(a).lower() for a in h.get("addresses") or [])]
        if addr in names:
            return k
    return None


def _secret_use(name: str) -> str:
    try:
        from aiforge_core.config import secure_store
        path = secure_store.security_dir() / "secrets" / name
        return f"$(cat {path})"
    except Exception:  # noqa: BLE001
        return f"<secret {name}>"


def _host_line(h: dict) -> str:
    bits = [f"**{h.get('name')}**"]
    if h.get("addresses"):
        bits.append("(" + ", ".join(h["addresses"]) + ")")
    if h.get("aliases"):
        bits.append("aka " + ", ".join(h["aliases"]))
    line = " ".join(bits)
    det = []
    if h.get("method"):
        det.append(f"access: {h['method']}")
    if h.get("user"):
        det.append(f"user {h['user']}")
    if h.get("port"):
        det.append(f"port {h['port']}")
    if h.get("key_path"):
        det.append(f"key {h['key_path']}")
    if h.get("secret"):
        det.append(f"password/token: {_secret_use(h['secret'])} (never print it)")
    if det:
        line += " — " + ", ".join(det)
    if h.get("recipe"):
        line += f"\n  works: `{h['recipe']}`"
    for a in h.get("avoid") or []:
        line += f"\n  do not: {a}"
    if h.get("notes"):
        line += f"\n  note: {h['notes']}"
    src = h.get("_source") or {}
    if src.get("method") == "user":
        line += "\n  (access method set by the user — use it as given)"
    return line


# ── repos ─────────────────────────────────────────────────────────────────
def upsert_repo(repo: str, fields: dict, *, source: str = "learned") -> dict:
    k = _key(repo)
    if not k:
        return {"ok": False, "error": "repo required"}
    with _LOCK:
        data = load()
        entry = data["repos"].get(k) or {"repo": repo}
        changed = _merge(entry, fields, source, REPO_FIELDS)
        data["repos"][k] = entry
        if changed:
            _save(data)
    return {"ok": True, "repo": k, "changed": changed}


_PY = re.compile(r"(?:^|[\s;&(])((?:\S*/)?(?:\.?venv|env|\.tox/\w+|\S*envs?/\S+)/bin/python3?(?:\.\d+)?)\b")
_PYPATH = re.compile(r"\bPYTHONPATH=(\S+)")
_CD = re.compile(r"^\s*cd\s+(\S+)\s*&&")
_TEST = re.compile(r"(?:-m\s+pytest|\bpytest\b|-m\s+unittest|\btox\b)")
_PROBE = re.compile(r"\s-c\s|<<|\bpython3?\s+-\s")


def _rel(path: str, root: str) -> str:
    path = path.strip("'\"")
    if root and os.path.isabs(path):
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            return path
        return "." if rel == "." else (rel if not rel.startswith("..") else path)
    return path


def learn_repo_command(repo: str, root: str, cmd: str) -> dict:
    """Learn the interpreter, env and test command from a command that worked.
    A one-off probe (``python -c``, a heredoc) teaches only the interpreter."""
    if not enabled() or not repo or not cmd:
        return {"ok": False}
    fields: dict = {}
    m = _PY.search(cmd)
    if m:
        fields["python"] = _rel(m.group(1), root)
    m = _PYPATH.search(cmd)
    if m:
        fields["env"] = f"PYTHONPATH={_rel(m.group(1), root)}"
    m = _CD.match(cmd)
    if m:
        cwd = _rel(m.group(1), root)
        if cwd != ".":
            fields["cwd"] = cwd
    if _TEST.search(cmd) and not _PROBE.search(cmd):
        body = cmd[_CD.match(cmd).end():].strip() if _CD.match(cmd) else cmd.strip()
        if root:
            body = body.replace(root.rstrip("/") + "/", "").replace(root, ".")
        # the narrowing part (one test, -k, -x) is this run's, not the recipe
        body = re.split(r"\s(?:-k|-x|--lf|::)\b|\s\S+::\S+", body)[0].strip()
        if "|" not in body and len(body) < 200:
            fields["test_cmd"] = body
    if not fields:
        return {"ok": False}
    return upsert_repo(repo, fields, source="learned")


# ── learning from the session's commands ──────────────────────────────────
_SSH = re.compile(r"(?:^|[\s;&|(])(?:sshpass\s+\S+(?:\s+\S+)?\s+)?ssh\s")
_ADDR = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_PARAMIKO = re.compile(r"\bparamiko\b")
_HOSTARG = re.compile(r"""(?:hostname|host)\s*=\s*['"]([\w.-]+)['"]|connect\(\s*['"]([\w.-]+)['"]""")


def learn_from_items(items: list[dict], *, repo: str = "", root: str = "") -> int:
    """Learn hosts and repo recipes from a session's executed commands
    (``session_ledger.ledger_items``). Returns how many entries changed."""
    if not enabled():
        return 0
    from aiforge_core.runtime.session_ledger import _ssh_target
    changed = 0
    failed_ssh: dict[str, str] = {}
    for item in items:
        key = str(item.get("key") or "")
        if not key.startswith("cmd:"):
            continue
        cmd = key[4:].strip()
        ok = item.get("outcome") is True
        target = _ssh_target(cmd) if _SSH.search(cmd) else None
        if target:
            dest = target.split()[-1]
            user, _, host = dest.rpartition("@")
            name = host_for_address(host) or host
            if not ok:
                failed_ssh[name] = cmd
                continue
            fields = {"recipe": f"ssh {target} '<command>'"}
            if user:
                fields["user"] = user
            fields["method"] = "password" if "sshpass" in cmd else "ssh_key"
            if "-i " in target:
                fields["key_path"] = target.split("-i ", 1)[1].split()[0]
            if _ADDR.fullmatch(host):
                fields["addresses"] = [host]
            if upsert_host(name, fields, source="learned").get("changed"):
                changed += 1
            continue
        if ok and _PARAMIKO.search(cmd):
            hosts = {a for a in _ADDR.findall(cmd)}
            hosts |= {g for m in _HOSTARG.finditer(cmd) for g in m.groups() if g}
            for host in hosts:
                name = host_for_address(host) or host
                fields = {"method": "paramiko",
                          "recipe": "a Python script with paramiko (SSHClient().connect(...))"}
                if _ADDR.fullmatch(host):
                    fields["addresses"] = [host]
                if name in failed_ssh:
                    fields["avoid"] = ["plain ssh failed here — use paramiko"]
                if upsert_host(name, fields, source="learned").get("changed"):
                    changed += 1
            continue
        if ok and repo:
            if learn_repo_command(repo, root, cmd).get("changed"):
                changed += 1
    return changed


# ── the prompt block ──────────────────────────────────────────────────────
def context_block(repo: str, text: str) -> str:
    """The hosts this request names and this repo's run recipe; "" if none."""
    if not enabled():
        return ""
    try:
        data = load()
    except Exception:  # noqa: BLE001
        return ""
    parts = []
    hosts = find_hosts(text)
    if hosts:
        parts.append("Hosts named in this request (how to reach them — saved "
                     "operational memory; the user's settings win over anything else):\n"
                     + "\n".join("- " + _host_line(h) for h in hosts[:6]))
    r = data["repos"].get(_key(repo)) if repo else None
    if r:
        lines = [f"How to run code in `{r.get('repo') or repo}` (worked before):"]
        for f, label in (("python", "python"), ("cwd", "run from"), ("env", "env"),
                         ("test_cmd", "tests"), ("run_cmd", "run")):
            if r.get(f):
                lines.append(f"- {label}: `{r[f]}`")
        if len(lines) > 1:
            parts.append("\n".join(lines))
    return "\n\n".join(parts)


# ── fixes: a failure seen before, and what turned it green ───────────────
_MAX_FIXES = 300
_FIX_TOOLS = ("run_tests", "run_command", "bash", "shell")


def _sig_key(sig: str) -> str:
    import hashlib
    return hashlib.sha1(sig.encode("utf-8", "replace")).hexdigest()[:16]


def _changed_since(st, snap: dict) -> list[str]:
    now = dict(getattr(st, "file_hashes", {}) or {})
    return sorted(p for p, h in now.items() if snap.get(p) != h)


def _functions_touched(root: str, files: list[str]) -> list[str]:
    """Function/class names from the diff's hunk headers (``@@ ... @@ def f``)."""
    import subprocess
    if not root or not files:
        return []
    try:
        p = subprocess.run(["git", "-C", root, "diff", "-U0", "HEAD", "--", *files],
                           capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.SubprocessError):
        return []
    names: list[str] = []
    for m in re.finditer(r"^@@[^@]*@@\s*(?:async\s+)?(?:def|class|func|fn|function|public|private|protected)?\s*([\w.]+)",
                         p.stdout, re.M):
        if m.group(1) not in names and len(m.group(1)) > 1:
            names.append(m.group(1))
    return names[:6]


def record_fix(sig: str, headline: str, *, repo: str, files: list[str],
               functions: list[str]) -> dict:
    if not sig or not files:
        return {"ok": False}
    k = _sig_key(sig)
    with _LOCK:
        data = load()
        e = data["fixes"].get(k) or {"signature": sig[:400], "count": 0}
        e.update({"headline": headline[:200], "repo": repo, "files": files[:8],
                  "functions": functions[:6], "updated": time.strftime("%Y-%m-%d")})
        e["count"] = int(e.get("count") or 0) + 1
        data["fixes"][k] = e
        if len(data["fixes"]) > _MAX_FIXES:      # oldest first out
            for old in sorted(data["fixes"], key=lambda x: data["fixes"][x].get("updated", ""))[
                    :len(data["fixes"]) - _MAX_FIXES]:
                data["fixes"].pop(old, None)
        _save(data)
    return {"ok": True, "fix": k}


def known_fix(sig: str, repo: str = "") -> "dict | None":
    e = load()["fixes"].get(_sig_key(sig)) if sig else None
    if not e:
        return None
    return e


def fix_note(e: dict, repo: str) -> str:
    where = "" if not e.get("repo") or e.get("repo") == repo else f" (in {e['repo']})"
    fn = f" — {', '.join(e['functions'])}" if e.get("functions") else ""
    return (f"This failure was seen before{where} and went green after changing "
            f"{', '.join(e.get('files') or [])}{fn}. Check that change first.")


def fix_hook(st, name: str, args: dict, result, cwd: str, repo: str = "") -> str:
    """Per tool result: remember failures; when a run goes green after edits,
    record what changed for each failure seen; when a known failure comes
    back, return the note for that result ("" otherwise)."""
    if not enabled() or name not in _FIX_TOOLS or not isinstance(result, dict):
        return ""
    if result.get("running") or result.get("timed_out") or result.get("stopped"):
        return ""
    from aiforge_core.runtime.failure_signature import failure_of, result_text, runner_green
    text = result_text(result)
    open_ = st.__dict__.setdefault("fix_open", {}) if hasattr(st, "__dict__") else {}
    if result.get("ok") is False:
        fail = failure_of(text)
        if not fail.signature:
            return ""
        open_.setdefault(fail.signature, (fail.headline, dict(getattr(st, "file_hashes", {}) or {})))
        e = known_fix(fail.signature, repo)
        said = st.__dict__.setdefault("fix_said", set())
        if e and fail.signature not in said:        # once per failure per turn
            said.add(fail.signature)
            return fix_note(e, repo)
        return ""
    if open_ and runner_green(text):
        root = repo_root(cwd)
        for sig, (headline, snap) in list(open_.items()):
            files = _changed_since(st, snap)
            if files:
                rel = [_rel(f, root) for f in files]
                record_fix(sig, headline, repo=repo, files=rel,
                           functions=_functions_touched(root, rel))
            open_.pop(sig, None)
    return ""


def learn_repo_location(repo: str, cwd: str) -> bool:
    """The folder a chat worked in becomes the repo's location in repos.json —
    only when that repo has none (a folder the user set is never replaced)."""
    if not enabled() or not repo or repo in ("repo", "chat") or repo.startswith("session-"):
        return False
    root = repo_root(cwd)
    if not root or not os.path.isdir(os.path.join(root, ".git")):
        return False
    try:
        from aiforge_core.config import repo_map
        if repo_map.get_path(repo):
            return False
        repo_map.set_path(repo, root)
        return True
    except Exception:  # noqa: BLE001
        return False


__all__ = ["enabled", "load", "upsert_host", "upsert_repo", "find_hosts",
           "learn_repo_command", "learn_from_items", "context_block", "METHODS",
           "fix_hook", "record_fix", "known_fix", "learn_repo_location"]
