"""Per-project command recipes: a verb (test, build, run, lint, deploy or a
user-named slug) → the exact command the project uses for it.

The user confirms a recipe once (card after a successful run, or a sentence
like "remember: compile = ./build.sh"); from then on the harness runs that
command for the verb and the model cannot route around it. Store:
``<repo>/.aiforge/recipes.json``. Off switch: ``AIFORGE_CHAT_RECIPES=0``."""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time

from aiforge_core.config import _atomic

log = logging.getLogger("aiforge.recipes")

BUILTIN_VERBS = ("test", "build", "run", "lint", "deploy")
_FILE = "recipes.json"
_DECLINED = "recipes-declined.json"
_MAX_BLOCK_LINES = 10

# Flags a family head may carry and still be the whole-project command:
# short (-q, -x) or long (--quiet, --tb=short). A flag with a separate value
# (-k foo, --maxfail 1) breaks the match on purpose — that is a targeted run.
_FLAGS = r"(?: -[a-zA-Z]+| --[a-z-]+(?:=\S+)?)*"
_MVN = r"^(?:\./)?mvnw?"

# Command families: whole-command patterns. A command with a path, a -k/-Dtest
# selector or anything after the family head is NOT a family command (the model
# is running one test file on purpose) — see family_of.
_FAMILY = (
    ("test", re.compile(r"^(?:python3? -m )?pytest" + _FLAGS + r"$")),
    ("test", re.compile(r"^(?:npm|yarn|pnpm) (?:run )?test$")),
    ("test", re.compile(_MVN + _FLAGS + r" (?:test|verify)$")),
    ("test", re.compile(r"^go test(?: \./\.\.\.)?$")),
    ("test", re.compile(r"^cargo test$")),
    ("test", re.compile(r"^make test$")),
    ("test", re.compile(r"^(?:\./)?gradlew? test$")),
    ("build", re.compile(_MVN + _FLAGS + r"(?: clean)? (?:compile|package|install)(?: -DskipTests)?$")),
    ("build", re.compile(r"^(?:npm|yarn|pnpm) run build$")),
    ("build", re.compile(r"^go build(?: \./\.\.\.)?$")),
    ("build", re.compile(r"^cargo build(?: --release)?$")),
    ("build", re.compile(r"^make(?: all| build)?$")),
    ("build", re.compile(r"^(?:npx )?tsc(?: --noEmit)?$")),
    ("build", re.compile(r"^(?:\./)?gradlew? (?:build|assemble)$")),
    ("lint", re.compile(r"^ruff(?: check)?(?: \.)?$")),
    ("lint", re.compile(r"^(?:npx )?eslint(?: \.)?$")),
    ("lint", re.compile(r"^(?:npm|yarn|pnpm) run lint$")),
    ("lint", re.compile(r"^flake8$")),
)

_PHRASES = {
    "test": ("test", "tests", "run tests", "run the tests", "run test", "run all tests"),
    "build": ("build", "build it", "compile", "compile it", "run build", "run the build"),
    "run": ("run", "run it", "run the app", "start it", "run the project"),
    "lint": ("lint", "run lint", "run the linter"),
    "deploy": ("deploy", "deploy it", "run deploy"),
}
_BUILTIN_PHRASES = {p for ps in _PHRASES.values() for p in ps}
# Tool heads that make "<verb> is <cmd>" a recipe sentence without a lead-in.
_TOOL_HEADS = frozenset((
    "python", "python3", "pytest", "npm", "yarn", "pnpm", "npx", "mvn", "mvnw",
    "gradle", "gradlew", "go", "cargo", "make", "tsc", "ruff", "eslint", "flake8",
    "docker", "bash", "sh"))
_POLITE = re.compile(r"^(?:please|pls|now|ok|okay|and|then|just|go ahead and)\s+", re.I)
_SAVE_RE = re.compile(
    r"^(?P<lead>remember:?|always|from now on|recipe:?)?,?\s*"
    r"(?:run |do )?(?P<verb>[a-z][a-z0-9-]{0,30})\s+"
    r"(?:recipe\s+)?(?P<sep>=|is|means|with|using|via)\s+"
    r"(?P<q>`?)(?P<cmd>[^`\n]{2,300}?)`?\s*[.!]?$", re.I)
_REMOVE_RE = re.compile(
    r"^(?:forget|remove|drop|delete)\s+(?:the\s+)?(?:(?P<before>recipe)\s+)?"
    r"(?P<verb>[a-z][a-z0-9-]{0,30})(?:\s+(?P<after>recipe))?\s*[.!]?$", re.I)
_CMD_START = re.compile(r"^(?:\.?/|[a-z][a-z0-9_.-]*)")


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_RECIPES", "1").strip().lower() not in (
        "0", "false", "no", "off")


def replace_enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_RECIPE_REPLACE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _dir(root: str) -> str:
    return os.path.join(root, ".aiforge")


def _path(root: str) -> str:
    return os.path.join(_dir(root), _FILE)


_WARNED: set[str] = set()   # store paths already reported unreadable (once per process)


def _read(path: str, *, for_write: bool = False) -> dict:
    """The JSON object in ``path``; {} when missing. On a read: any other
    failure is logged once and gives {}. Before a write: a read error other
    than "missing" is re-raised so the caller never overwrites a store it could
    not read, and a corrupt file is moved aside to ``<path>.bad-<epoch>`` so the
    rewrite does not bury what the user had there."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        if for_write:
            log.warning("recipes: cannot read %s before writing it: %s", path, exc)
            raise
        return _warn_once(path, f"cannot read ({exc})")
    except ValueError as exc:
        return _corrupt(path, f"not valid JSON ({exc})", for_write)
    if not isinstance(data, dict):
        return _corrupt(path, "top level is not an object", for_write)
    return data


def _warn_once(path: str, why: str) -> dict:
    if path not in _WARNED:
        _WARNED.add(path)
        log.warning("recipes: ignoring %s: %s", path, why)
    return {}


def _corrupt(path: str, why: str, for_write: bool) -> dict:
    if not for_write:
        return _warn_once(path, why)
    bad = f"{path}.bad-{int(time.time())}"
    try:
        os.replace(path, bad)
        log.warning("recipes: %s: %s; moved aside to %s", path, why, bad)
    except OSError as exc:
        log.warning("recipes: %s: %s; could not move aside (%s)", path, why, exc)
        raise
    return {}


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic.write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def load(root: str) -> dict:
    """verb → {cmd, cwd, aliases, saved}; {} when off or unreadable."""
    if not enabled() or not root:
        return {}
    out = {}
    for verb, row in _read(_path(root)).items():
        key = _slug(verb)
        if not key or not isinstance(row, dict) or not str(row.get("cmd") or "").strip():
            continue
        cwd = str(row.get("cwd") or ".").strip() or "."
        if not _cwd_ok(cwd):
            log.warning("recipes: ignoring %r: cwd %r leaves the project", key, cwd)
            continue
        out[key] = {"cmd": str(row["cmd"]).strip(),
                    "cwd": cwd,
                    "aliases": [str(a).strip().lower() for a in (row.get("aliases") or [])
                                if str(a).strip()],
                    "saved": row.get("saved")}
    return out


def get(root: str, verb: str) -> dict | None:
    return load(root).get(_slug(verb))


def _need_slug(verb: str) -> str:
    key = _slug(verb)
    if not key:
        raise ValueError(f"recipe verb must be a word or slug, got {verb!r}")
    return key


def save(root: str, verb: str, cmd: str, *, cwd: str = ".", aliases=None) -> dict:
    """Store ``verb → cmd``. ValueError on an empty verb or command, a cwd
    that leaves the project, or an alias that already means a built-in verb."""
    key = _need_slug(verb)
    cmd = str(cmd or "").strip()
    if not cmd:
        raise ValueError(f"recipe {key!r} needs a command")
    cwd = str(cwd or ".").strip() or "."
    if not _cwd_ok(cwd):
        raise ValueError(f"recipe cwd must stay inside the project, got {cwd!r}")
    row = {"cmd": cmd, "cwd": cwd, "saved": int(time.time())}
    clean = [str(a).strip().lower() for a in (aliases or []) if str(a).strip()]
    for alias in clean:
        if alias in _BUILTIN_PHRASES and alias not in _PHRASES.get(key, ()):
            raise ValueError(f"alias {alias!r} already means a built-in verb")
    if clean:
        row["aliases"] = clean
    data = _read(_path(root), for_write=True)
    data[key] = row
    _write(_path(root), data)
    return row


def remove(root: str, verb: str) -> bool:
    key = _need_slug(verb)
    data = _read(_path(root), for_write=True)
    if key not in data:
        return False
    del data[key]
    _write(_path(root), data)
    return True


def decline(root: str, verb: str) -> None:
    key = _need_slug(verb)
    p = os.path.join(_dir(root), _DECLINED)
    data = _read(p, for_write=True)
    data[key] = int(time.time())
    _write(p, data)


def _declined(root: str) -> set:
    return set(_read(os.path.join(_dir(root), _DECLINED)))


def _cwd_ok(cwd: str) -> bool:
    """A recipe cwd stays inside the project: relative, no ``..``."""
    return not (os.path.isabs(cwd) or ".." in cwd)


def exec_root(cwd: str) -> str:
    """Where a recipe RUNS: the toplevel of the checkout ``cwd`` is in (a chat
    worktree stays a worktree — unlike ``repo_ident.git_toplevel``, which maps
    it to the main repo and is right only for the recipes.json LOOKUP); ``cwd``
    itself outside git."""
    if not cwd:
        return cwd
    try:
        out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=3)
        top = out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — never let git break a recipe run
        top = ""
    return top or str(cwd)


def recipe_command(rec: dict, exec_root: str) -> str:
    """The shell line that runs ``rec`` from ``exec_root``: the command alone
    for cwd ".", else ``cd <abs dir> && <cmd>`` — so the recipe's folder holds
    on both the tool path and the raw-command rewrite, and under a workspace
    jail (where the shell tool ignores its cwd argument)."""
    cmd = str(rec.get("cmd") or "").strip()
    cwd = str(rec.get("cwd") or ".").strip() or "."
    if os.path.normpath(cwd) == ".":
        return cmd
    where = os.path.normpath(os.path.join(exec_root, cwd))
    return f"cd {shlex.quote(where)} && {cmd}"


def _slug(verb: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(verb or "").strip().lower()).strip("-")


def _canon(verb: str) -> str:
    """The built-in verb a sentence word stands for ("compile" → build,
    "tests" → test); any other word stays a user slug."""
    s = _slug(verb)
    spaced = s.replace("-", " ")
    for builtin, phrases in _PHRASES.items():
        if s in phrases or spaced in phrases:
            return builtin
    return s


def _norm(cmd: str) -> str:
    return " ".join(str(cmd or "").split())


def family_of(cmd: str) -> str | None:
    """The built-in verb a whole command belongs to, or None for targeted or
    unrelated commands."""
    c = _norm(cmd)
    for verb, rx in _FAMILY:
        if rx.match(c):
            return verb
    return None


def _match(root: str, cmd: str) -> tuple[str, str] | None:
    verb = family_of(cmd)
    if not verb:
        return None
    rec = get(root, verb)
    if not rec or _norm(rec["cmd"]) == _norm(cmd):
        return None
    return verb, rec["cmd"]


def rewrite(root: str, cmd: str) -> tuple[str, str] | None:
    """(verb, recipe command) when a raw family command must run as the recipe."""
    if not replace_enabled():
        return None
    return _match(root, cmd)


def warn(root: str, cmd: str) -> str | None:
    """The verb whose recipe the raw command ignores (warning mode only)."""
    if replace_enabled():
        return None
    m = _match(root, cmd)
    return m[0] if m else None


def verb_for_phrase(root: str, text: str) -> str | None:
    """The verb a bare message asks for, when a recipe for it exists. Built-in
    verbs are checked before custom ones so an alias never shadows them."""
    recs = load(root)
    if not recs:
        return None
    t = _norm(text).lower().strip(" .!,")
    while True:
        stripped = _POLITE.sub("", t)
        if stripped == t:
            break
        t = stripped
    if not t or "?" in t or len(t) > 40:
        return None
    ordered = [v for v in BUILTIN_VERBS if v in recs] + [v for v in recs if v not in BUILTIN_VERBS]
    for verb in ordered:
        phrases = set(_PHRASES.get(verb, ())) | set(recs[verb]["aliases"])
        phrases |= {verb, f"run {verb}", f"run the {verb}", verb.replace("-", " ")}
        if t in phrases:
            return verb
    return None


def _looks_like_command(verb: str, cmd: str) -> bool:
    """A plain "<verb> is <cmd>" (no lead-in, no = or backticks) only counts
    when the verb is a built-in or a coined slug (run-job) and the command is
    a tool head with arguments or a path — "build is slow", "run with docker",
    "go with docker compose" and "url is https://x" are prose."""
    if "://" in cmd:
        return False
    if verb not in BUILTIN_VERBS and "-" not in verb:
        return False
    parts = cmd.split()
    head = parts[0].lower() if parts else ""
    if head.startswith(("./", "/", "~")):
        return True
    return len(parts) >= 2 and head in _TOOL_HEADS


def parse_sentence(text: str) -> tuple[str, str, str] | None:
    """("save", verb, cmd) / ("remove", verb, "") for a one-line recipe sentence."""
    t = _norm(text)
    if not t or "\n" in text.strip() or len(t) > 320:
        return None
    m = _REMOVE_RE.match(t)
    if m:
        if not (m.group("before") or m.group("after")):
            return None
        return "remove", _canon(m.group("verb")), ""
    m = _SAVE_RE.match(t)
    if not m:
        return None
    verb, cmd = _canon(m.group("verb")), m.group("cmd").strip()
    if verb in ("that", "this", "the", "it") or not _CMD_START.match(cmd):
        return None
    if " is what " in t or " says" in t or verb in ("remember", "always"):
        return None
    explicit = bool(m.group("lead")) or m.group("sep") == "=" or bool(m.group("q"))
    if not explicit and not _looks_like_command(verb, cmd):
        return None
    return "save", verb, cmd


def offers(root: str, steps) -> list[dict]:
    """Recipe offers for this turn: one per family verb that ran green and has
    no recipe and was not declined. Order = first success."""
    if not enabled():
        return []
    have, declined, out, seen = load(root), _declined(root), [], set()
    for s in steps or []:
        if not isinstance(s, dict) or s.get("type") != "tool":
            continue
        if s.get("name") not in ("run_command", "bash", "run_shell", "shell"):
            continue
        res = s.get("result") if isinstance(s.get("result"), dict) else {}
        if res.get("ok") is not True:
            continue
        args = s.get("args") if isinstance(s.get("args"), dict) else {}
        cmd = _norm(args.get("cmd") or args.get("command"))
        verb = family_of(cmd)
        if not verb or verb in have or verb in declined or verb in seen:
            continue
        seen.add(verb)
        out.append({"verb": verb, "cmd": cmd, "cwd": _offer_cwd(root, args.get("cwd"))})
        if len(out) == 3:
            break
    return out


def _offer_cwd(root: str, cwd) -> str:
    """The step's cwd as a path inside the project: relative stays, absolute
    under ``root`` becomes relative, anything that leaves the project is "."."""
    cwd = str(cwd or ".").strip() or "."
    if os.path.isabs(cwd):
        try:
            cwd = os.path.relpath(cwd, root or os.curdir)
        except ValueError:
            return "."
    cwd = os.path.normpath(cwd)
    if os.path.isabs(cwd) or cwd == ".." or ".." in cwd.replace("\\", "/").split("/"):
        return "."
    return cwd


def help_text(root: str) -> str:
    recs = load(root)
    if not recs:
        return "No recipes saved for this project."
    lines = ["Recipes for this project:"]
    for verb, rec in recs.items():
        extra = f"  (aliases: {', '.join(rec['aliases'])})" if rec["aliases"] else ""
        lines.append(f"- {verb} → {rec['cmd']}{extra}")
    return "\n".join(lines)


def block(root: str) -> str:
    """Prompt block (priority tier). Empty when there are no recipes."""
    recs = load(root)
    if not recs:
        return ""
    if replace_enabled():
        rule = "run the recipe command exactly (the harness replaces a raw command with it):"
    else:
        rule = "run the recipe command, not a raw equivalent:"
    lines = [f"RECIPES — this project's fixed commands. For these verbs {rule}"]
    verbs = list(recs)
    for verb in verbs[:_MAX_BLOCK_LINES]:
        lines.append(f"- {verb} → {recs[verb]['cmd']}")
    if len(verbs) > _MAX_BLOCK_LINES:
        lines.append("more: " + ", ".join(verbs[_MAX_BLOCK_LINES:]))
    return "\n".join(lines)
