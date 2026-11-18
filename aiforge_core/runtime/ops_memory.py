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
import subprocess
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
    data.setdefault("pending", {})
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
    lw = h.get("last_ok") or {}
    if lw.get("cmd"):
        line += f"\n  LAST WORKED ({lw.get('at', '')}): `{lw['cmd']}` — use this first"
    elif h.get("recipe"):
        line += f"\n  works: `{h['recipe']}`"
    lf = h.get("last_fail") or {}
    if lf.get("cmd") and lf.get("at", "") > lw.get("at", ""):
        line += f"\n  last failed ({lf.get('at', '')}): `{lf['cmd']}` — {lf.get('error', '')}"
    for a in h.get("avoid") or []:
        line += f"\n  do not: {a}"
    if h.get("notes"):
        line += f"\n  note: {h['notes']}"
    src = h.get("_source") or {}
    if src.get("method") == "user":
        line += ("\n  (access method set by the user — use exactly this for this host, "
                 "do not try another method first; other hosts keep their own)")
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
# Any toolchain's test command, not only Python's: a working
# "JAVA_HOME=... mvn -o test" was never learned, so every session rediscovered it.
_TEST = re.compile(
    r"(?:-m\s+pytest|\bpytest\b|-m\s+unittest|\btox\b"
    r"|(?:\bmvnd|\bmvn|\bmvnw|\./mvnw)\b[^|;&]*\b(?:test|verify)\b"
    r"|(?:\bgradle|\bgradlew|\./gradlew)\b[^|;&]*\b(?:test|check)\b"
    r"|\b(?:npm|yarn|pnpm|bun)\s+(?:run\s+)?test\b"
    r"|\bgo\s+test\b|\bcargo\s+(?:test|nextest)\b|\bdotnet\s+test\b"
    r"|\bmake\b[^|;&]*\b(?:test|check)\b|\bmeson\s+test\b|\bbazel\s+test\b|\s--gtest_\w+"
    r"|\b(?:jest|vitest|mocha|rspec|phpunit|ctest)\b)")
# Leading VAR=value assignments of the command (JAVA_HOME, PATH=...:$PATH, ...).
_ENV_PREFIX = re.compile(r"^((?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S+)\s+)+)")
# The narrowing part of a run (one test, a filter) is this run's, not the recipe.
_NARROW = re.compile(
    r"\s(?:-k|-x|--lf|::)\b|\s\S+::\S+|\s-Dtest=\S+|\s--tests?\s+\S+"
    r"|\s-run\s+\S+|\s-t\s+\S+|\s--testNamePattern\S*|\s--gtest_filter=\S+|\s-R\s+\S+"
    r"|(?<=cargo test)\s+(?!-)\S+|(?<=nextest run)\s+(?!-)\S+|\s--(?=\s|$)")
# Output trimming after the command is not part of the recipe.
_TRIM = re.compile(r"\s*(?:2>&1\s*)?(?:\|\s*(?:tail|head|grep|tee|less|cat)\b.*)?$")
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
    m = _CD.match(cmd)
    if m:
        cwd = _rel(m.group(1), root)
        if cwd != ".":
            fields["cwd"] = cwd
    body = cmd[m.end():].strip() if m else cmd.strip()
    env = _ENV_PREFIX.match(body)
    if env:
        fields["env"] = " ".join(
            (f"{k}={_rel(v, root)}" if k == "PYTHONPATH" else f"{k}={v}")
            for k, _, v in (a.partition("=") for a in env.group(1).split()))
        body = body[env.end():].strip()
    else:
        mp = _PYPATH.search(cmd)
        if mp:
            fields["env"] = f"PYTHONPATH={_rel(mp.group(1), root)}"
    if _TEST.search(body) and not _PROBE.search(body):
        if root:
            body = body.replace(root.rstrip("/") + "/", "").replace(root, ".")
        body = _TRIM.sub("", _NARROW.split(body)[0]).strip()
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


# ── what the user says about a host ───────────────────────────────────────
# "10.0.0.5 use the ssh key; the other one 10.0.0.6 username/password": the
# model was trusted to call remember_host, and often wrote a loose note or
# nothing, so the next turn mixed the two hosts up. Each user clause naming
# an address and ONE access method is saved as the user's setting — no
# model call, and nothing learned from a command can overwrite it.
_USER_AT = re.compile(r"\b([A-Za-z_][\w.-]{0,31})@([A-Za-z0-9][\w.-]*[A-Za-z0-9])\b")
_KEY_WORDS = re.compile(
    r"\b(?:ssh[- ]?keys?|key[- ]?based|private[- ]key|keys?|pem|id_rsa|id_ed25519|"
    r"identity[- ]file|pubkey|public[- ]key)\b", re.I)
_PASS_WORDS = re.compile(
    r"\b(?:passwords?|passwd|pass|pwd|user\s*name\s*(?:and|&|/)\s*pass\w*|"
    r"user\s*/\s*pass\w*|credentials?|sshpass)\b", re.I)
_NEGATED = re.compile(r"\b(?:not|no|never|without|instead\s+of|don'?t\s+use)\s+(?:\w+\s+){0,2}$", re.I)
_USER_NAMED = re.compile(r"\b(?:user(?:name)?|login)\s*(?:is|=|:|-)?\s*([A-Za-z_][\w.-]{0,31})", re.I)
_NOT_USER = {"and", "or", "password", "pass", "name", "names", "the", "is", "with", "to",
             "for", "a", "an", "pwd", "passwd", "credentials", "credential", "and/password"}


_PASS_VALUE = re.compile(
    r"\b(?:password|passwd|pwd|pass)\s*(?:is|=|:)\s*['\"`]?([^\s'\"`]{1,128})", re.I)


def _store_secret(name: str, value: str, purpose: str = "") -> str:
    """Write a password the user typed into the secret store (0600 file in
    the 0700 security folder). Returns the secret's NAME; "" on failure.
    Only the name is ever kept in this memory."""
    value = (value or "").strip().rstrip(".,;")
    if not value or not re.fullmatch(r"[\w.-]{1,64}", name):
        return ""
    try:
        from aiforge_core.config import _atomic, secure_store
        folder = secure_store.security_dir(create=True) / "secrets"
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / name
        _atomic.write_text(str(path), value)
        os.chmod(path, 0o600)
        return name
    except Exception:  # noqa: BLE001
        return ""


def _says(rx: "re.Pattern", clause: str) -> bool:
    """``rx`` occurs in ``clause`` not right after a negation."""
    return any(not _NEGATED.search(clause[:m.start()]) for m in rx.finditer(clause))


def capture_from_text(text: str) -> int:
    """Save the access method the user states for each host named in
    ``text`` (their own words). Returns how many hosts changed."""
    if not enabled() or not text:
        return 0
    changed = 0
    for clause in re.split(r"(?<=[!?;])\s+|\.\s+(?=[^\d])|\n+", text):
        targets: dict = {}
        for m in _USER_AT.finditer(clause):
            targets[m.group(2).rstrip(".")] = m.group(1)
        for a in _ADDR.findall(clause):
            targets.setdefault(a, "")
        if not targets:
            continue
        key, pw = _says(_KEY_WORDS, clause), _says(_PASS_WORDS, clause)
        if key == pw:                       # neither, or both: not a clear statement
            continue
        method = "ssh_key" if key else "password"
        pw_val = _PASS_VALUE.search(clause)
        named = next((u for u in (m.group(1).rstrip(".-") for m in _USER_NAMED.finditer(clause))
                      if u.lower() not in _NOT_USER), "")
        for host, user in targets.items():
            if not (_ADDR.fullmatch(host) or "." in host or host_for_address(host)):
                continue                    # "me@home" in prose is not a host
            name = host_for_address(host) or host
            fields = {"method": method}
            if _ADDR.fullmatch(host):
                fields["addresses"] = [host]
            if user or named:
                fields["user"] = user or named
            if pw_val and method == "password":
                sec = _store_secret(f"host-{_key(name)}-password", pw_val.group(1),
                                    f"password for {name}")
                if sec:
                    fields["secret"] = sec
            if upsert_host(name, fields, source="user").get("changed"):
                changed += 1
    return changed


# ── per step: what reached a host, and what did not ──────────────────────
_ACCESS_FAIL = re.compile(
    r"permission denied|authentication failed|auth(?:entication)? fail|connection (?:refused|timed out|reset|closed)|"
    r"no route to host|could not resolve|host key verification failed|too many authentication|"
    r"timed out|password:\s*$|incorrect password|access denied|unreachable", re.I | re.M)
_INLINE_PW = re.compile(r"(sshpass\s+-p\s*)(\S+)|(password\s*=\s*)(['\"])[^'\"]*\4", re.I)


def _redact(cmd: str) -> str:
    def rep(m):
        if m.group(1):
            return m.group(1) + "<password>"
        return m.group(3) + m.group(4) + "<password>" + m.group(4)
    return _INLINE_PW.sub(rep, cmd)


def _shape(cmd: str) -> str:
    """The reusable form of a command that reached a host: ssh target kept,
    the remote command replaced; passwords never kept."""
    from aiforge_core.runtime.session_ledger import _ssh_target
    cmd = " ".join(_redact(cmd).split())
    target = _ssh_target(cmd) if _SSH.search(cmd) else None
    if target:
        pre = cmd.split("ssh ", 1)[0] if cmd.lstrip().startswith("sshpass") else ""
        return f"{pre}ssh {target} '<command>'".strip()
    q = min([i for i in (cmd.find("'"), cmd.find('"')) if i > 0] or [len(cmd)])
    head = cmd[:q].rstrip()
    return (head + " '<command>'" if q < len(cmd) else head)[:240]


def _targets(cmd: str) -> list:
    """(name, address) of hosts this command reaches: an ssh target, an
    address or a saved host's name/alias in the text."""
    from aiforge_core.runtime.session_ledger import _ssh_target
    out: dict = {}
    t = _ssh_target(cmd) if _SSH.search(cmd) else None
    if t:
        host = t.split()[-1].rpartition("@")[2]
        out[host_for_address(host) or host] = host
    for h in find_hosts(cmd):
        out.setdefault(_key(h.get("name") or ""), "")
    if _SSH.search(cmd) or _PARAMIKO.search(cmd) or re.search(r"\b(?:scp|rsync|sftp)\b", cmd):
        for a in _ADDR.findall(cmd):
            out.setdefault(host_for_address(a) or a, a)
    return [(k, v) for k, v in out.items() if k]


def host_hook(name: str, args: dict, result) -> int:
    """After a shell step: for each host it reached, keep what LAST WORKED;
    an access failure is kept as last failed (and avoided). Returns changes."""
    if not enabled() or name not in _FIX_TOOLS or not isinstance(result, dict):
        return 0
    try:
        from aiforge_core.runtime.shell_writes import command_of
        cmd = command_of(args or {}) or ""
    except Exception:  # noqa: BLE001
        return 0
    if not cmd.strip():
        return 0
    targets = _targets(cmd)
    if not targets:
        return 0
    code = result.get("code", result.get("exit_code"))
    ok = bool(result.get("ok", True)) and code in (0, None) and not result.get("timed_out")
    out = str(result.get("stderr") or "") + "\n" + str(result.get("stdout") or "")[-2000:]
    shape, today = _shape(cmd), time.strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    for key, addr in targets:
        with _LOCK:
            data = load()
            entry = data["hosts"].get(_key(key)) or {"name": key}
            if addr and _ADDR.fullmatch(addr):
                entry.setdefault("addresses", [])
                if addr not in entry["addresses"]:
                    entry["addresses"].append(addr)
            if ok:
                entry["last_ok"] = {"cmd": shape, "at": today}
                entry["avoid"] = [a for a in entry.get("avoid") or [] if not a.startswith(shape)]
                src = entry.setdefault("_source", {})
                if src.get("method") != "user":
                    m = ("password" if "sshpass" in cmd else "paramiko" if _PARAMIKO.search(cmd)
                         else "ssh_key" if _SSH.search(cmd) else entry.get("method"))
                    if m:
                        entry["method"] = m
                        src.setdefault("method", "learned")
            else:
                hit = _ACCESS_FAIL.search(out)
                if not hit:
                    continue                 # the host was reached; the command itself failed
                err = out[max(0, hit.start() - 40):hit.end() + 40].strip().splitlines()
                err = (err[-1] if err else hit.group(0))[:120]
                entry["last_fail"] = {"cmd": shape, "at": today, "error": err}
                av = [a for a in entry.get("avoid") or [] if not a.startswith(shape)]
                entry["avoid"] = (av + [f"{shape}: {err}"])[-_MAX_AVOID:]
            entry["updated"] = today[:10]
            data["hosts"][_key(key)] = entry
            _save(data)
            n += 1
    return n


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
        parts.append("Hosts named in this chat (how to reach them — saved operational "
                     "memory). For each host try, in order: what LAST WORKED for it, "
                     "then the access method set for it; never repeat what is listed "
                     "as failing. Each host keeps its own method:\n"
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
    for m in re.finditer(r"^@@[^@]*@@[ \t]*(.+)$", p.stdout, re.M):
        name = _hunk_name(m.group(1))
        if name and name not in names:
            names.append(name)
    return names[:6]


#: Words in a hunk header that are never the name (modifiers, keywords, types).
_NOT_NAME = frozenset(
    "public private protected internal static final abstract synchronized native "
    "async await export default pub fn def func function class interface enum struct "
    "impl trait record object const let var val override virtual inline extern "
    "unsafe void int long bool boolean char double float string auto return".split())


def _hunk_name(header: str) -> str:
    """The function/class a hunk header names: the identifier right before
    "(", else the identifier after class/struct/interface/enum/impl/trait.
    Java's "public final class AgingCalculator {" read as "final"."""
    m = re.search(r"([A-Za-z_][\w.:]*)\s*(?:<[^>]*>)?\s*\(", header)
    if m and m.group(1).split(".")[-1].lower() not in _NOT_NAME:
        return m.group(1).split("::")[-1]
    m = re.search(r"\b(?:class|struct|interface|enum|impl|trait|record|object)\s+([A-Za-z_]\w*)", header)
    if m:
        return m.group(1)
    words = [w for w in re.findall(r"[A-Za-z_]\w*", header) if w.lower() not in _NOT_NAME]
    return words[0] if words and len(words[0]) > 1 else ""


def _parts(sig: str) -> list[str]:
    """The separate errors of a signature ("a|b|c" lists several)."""
    return [p.strip() for p in (sig or "").split("|") if p.strip()]


def _change_summary(root: str, files: list[str], limit: int = 800, base: str = "HEAD") -> str:
    """What the fix changed: the added/removed lines of ``git diff`` for those
    files (no context), so a later run reads the fix, not only where it was."""
    if not root or not files:
        return ""
    try:
        p = subprocess.run(["git", "-C", root, "diff", "-U0", "--no-color", base or "HEAD", "--", *files[:8]],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    out: list[str] = []
    for ln in (p.stdout or "").splitlines():
        if ln.startswith("+++ "):
            out.append("# " + ln[6:] if ln.startswith("+++ b/") else ln)
        elif (ln.startswith("+") or ln.startswith("-")) and not ln.startswith("---"):
            if ln[1:].strip():
                out.append(ln[:160])
    text = "\n".join(out)
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n…"


def record_fix(sig: str, headline: str, *, repo: str, files: list[str],
               functions: list[str], change: str = "") -> dict:
    if not sig or not files:
        return {"ok": False}
    k = _sig_key(sig)
    with _LOCK:
        data = load()
        e = data["fixes"].get(k) or {"signature": sig[:400], "count": 0}
        e.update({"headline": headline[:200], "repo": repo, "files": files[:8],
                  "functions": functions[:6], "updated": time.strftime("%Y-%m-%d"),
                  "parts": [_sig_key(x) for x in _parts(sig)][:12]})
        if change:
            e["change"] = change
        e["count"] = int(e.get("count") or 0) + 1
        data["fixes"][k] = e
        if len(data["fixes"]) > _MAX_FIXES:      # oldest first out
            for old in sorted(data["fixes"], key=lambda x: data["fixes"][x].get("updated", ""))[
                    :len(data["fixes"]) - _MAX_FIXES]:
                data["fixes"].pop(old, None)
        _save(data)
    return {"ok": True, "fix": k}


#: Failures still open after their turn: resolved by a later green run.
_MAX_PENDING = 50
_PENDING_DAYS = 7


def _snapshot(root: str) -> str:
    """A ref for the code as it is now (uncommitted edits included)."""
    try:
        p = subprocess.run(["git", "-C", root, "stash", "create"], capture_output=True,
                           text=True, timeout=10)
        ref = p.stdout.strip()
        if not ref:
            h = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True,
                               text=True, timeout=10)
            ref = h.stdout.strip()
        return ref
    except (OSError, subprocess.SubprocessError):
        return ""


def _cmd_key(args: dict) -> str:
    try:
        from aiforge_core.runtime.shell_writes import command_of
        cmd = command_of(args or {}) or ""
        from aiforge_core.runtime.chat_agent._shell import test_identity
        return test_identity(cmd) or cmd
    except Exception:  # noqa: BLE001
        return ""


def _pending_add(sig: str, headline: str, repo: str, root: str, cmd: str) -> None:
    """Keep a failure that is still open, with a ref of the code at that point."""
    if not (sig and repo and root and cmd):
        return
    k = _sig_key(sig)
    with _LOCK:
        data = load()
        pend = data["pending"]
        if k in pend and pend[k].get("repo") == repo:
            return                                   # the first sighting is the base
        ref = _snapshot(root)
        if not ref:
            return
        pend[k] = {"signature": sig[:400], "headline": headline[:200], "repo": repo,
                   "cmd": cmd[:300], "ref": ref, "at": time.time()}
        if len(pend) > _MAX_PENDING:
            for old in sorted(pend, key=lambda x: pend[x].get("at", 0))[:len(pend) - _MAX_PENDING]:
                pend.pop(old, None)
        _save(data)


def _pending_resolve(repo: str, root: str, cmd: str, skip: set) -> int:
    """A green run of the same command in the same repo, in a later turn:
    record each still-open failure of it as fixed by what changed since."""
    if not (repo and root and cmd):
        return 0
    with _LOCK:
        data = load()
        pend = data["pending"]
        due = [(k, e) for k, e in pend.items()
               if e.get("repo") == repo and e.get("cmd") == cmd and k not in skip]
    n = 0
    for k, e in due:
        with _LOCK:
            data = load()
            data["pending"].pop(k, None)
            _save(data)
        if time.time() - float(e.get("at") or 0) > _PENDING_DAYS * 86400:
            continue
        try:
            p = subprocess.run(["git", "-C", root, "diff", "--name-only", e["ref"]],
                               capture_output=True, text=True, timeout=15)
            files = [f for f in p.stdout.splitlines() if f.strip()] if p.returncode == 0 else []
        except (OSError, subprocess.SubprocessError):
            files = []
        if files:
            record_fix(e["signature"], e.get("headline", ""), repo=repo, files=files[:8],
                       functions=_functions_touched(root, files[:8]),
                       change=_change_summary(root, files[:8], base=e["ref"]))
            n += 1
    return n


def fixes_for(repo: str = "") -> list:
    """Known fixes (one repo, or all), newest first, for the UI."""
    out = []
    for k, e in load()["fixes"].items():
        if repo and e.get("repo") != repo:
            continue
        out.append({"id": k, **{x: e.get(x) for x in (
            "headline", "repo", "files", "functions", "change", "count", "updated", "signature")}})
    return sorted(out, key=lambda x: str(x.get("updated") or ""), reverse=True)


def forget_fix(fix_id: str) -> bool:
    with _LOCK:
        data = load()
        if data["fixes"].pop(fix_id, None) is None:
            return False
        _save(data)
    return True


def known_fix(sig: str, repo: str = "") -> "dict | None":
    """The fix recorded for this failure: the same signature, else the fix
    sharing the most separate errors with it (same repo first). A signature
    lists several errors; one extra or missing error used to make a known
    fix unfindable."""
    if not sig:
        return None
    fixes = load()["fixes"]
    e = fixes.get(_sig_key(sig))
    if e:
        return e
    mine = {_sig_key(x) for x in _parts(sig)}
    if not mine:
        return None
    best, score = None, (0, 0)
    for cand in fixes.values():
        shared = len(mine & set(cand.get("parts") or []))
        if not shared:
            continue
        rank = (shared, 1 if (repo and cand.get("repo") == repo) else 0)
        if rank > score:
            best, score = cand, rank
    return best


def fix_note(e: dict, repo: str) -> str:
    where = "" if not e.get("repo") or e.get("repo") == repo else f" (in {e['repo']})"
    fn = f" — {', '.join(e['functions'])}" if e.get("functions") else ""
    change = (e.get("change") or "").strip()
    return (f"This failure was seen before{where} and went green after changing "
            f"{', '.join(e.get('files') or [])}{fn}. Check that change first."
            + (f" What changed then:\n{change}" if change else ""))


def fix_hook(st, name: str, args: dict, result, cwd: str, repo: str = "") -> str:
    """Per tool result: remember failures; when a run goes green after edits,
    record what changed for each failure seen; when a known failure comes
    back, return the note for that result ("" otherwise)."""
    if not enabled() or not isinstance(result, dict):
        return ""
    # A long build goes to the background: its start says "running" and its
    # end comes back through command_wait. Judge that end as the command's
    # own run — before, a Maven fix (always backgrounded) was never recorded.
    try:
        from aiforge_core.runtime import cmd_finished
        run = cmd_finished.as_run(name, args, result)
    except Exception:  # noqa: BLE001
        run = (name, args, result)
    if run is None:
        return ""
    name, args, result = run
    if name not in _FIX_TOOLS or not isinstance(result, dict):
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
        try:   # kept past this turn: a later turn may fix it
            _pending_add(fail.signature, fail.headline, repo, repo_root(cwd), _cmd_key(args))
        except Exception:  # noqa: BLE001
            pass
        e = known_fix(fail.signature, repo)
        said = st.__dict__.setdefault("fix_said", set())
        if e and fail.signature not in said:        # once per failure per turn
            said.add(fail.signature)
            return fix_note(e, repo)
        return ""
    # Green: the runner says so, or a clean exit with no failure in its output
    # ("mvn -q test" prints nothing at all when every test passes).
    green = runner_green(text) or (result.get("ok") is True
                                   and not failure_of(text).signature)
    if green:
        root = repo_root(cwd)
        done = set()
        for sig, (headline, snap) in list(open_.items()):
            files = _changed_since(st, snap)
            if files:
                rel = [_rel(f, root) for f in files]
                record_fix(sig, headline, repo=repo, files=rel,
                           functions=_functions_touched(root, rel),
                           change=_change_summary(root, rel))
            open_.pop(sig, None)
            done.add(_sig_key(sig))
            with _LOCK:
                d = load()
                if d["pending"].pop(_sig_key(sig), None) is not None:
                    _save(d)
        try:   # failures left open by EARLIER turns, fixed now
            _pending_resolve(repo, root, _cmd_key(args), done)
        except Exception:  # noqa: BLE001
            pass
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
           "capture_from_text", "host_hook",
           "fix_hook", "record_fix", "known_fix", "fixes_for", "forget_fix",
           "learn_repo_location"]
