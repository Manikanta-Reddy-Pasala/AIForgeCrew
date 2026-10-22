"""Per-project command recipes: a verb (test, build, run, lint, deploy or a
user-named slug) → the exact command the project uses for it.

The user confirms a recipe once (card after a successful run, or a sentence
like "remember: compile = ./build.sh"); from then on the harness runs that
command for the verb and the model cannot route around it. Store:
``<repo>/.aiforge/recipes.json``. Off switch: ``AIFORGE_CHAT_RECIPES=0``."""
from __future__ import annotations

import json
import os
import re
import time

from aiforge_core.config import _atomic

BUILTIN_VERBS = ("test", "build", "run", "lint", "deploy")
_FILE = "recipes.json"
_DECLINED = "recipes-declined.json"
_MAX_BLOCK_LINES = 10

# Command families: whole-command patterns. A command with a path, a -k/-Dtest
# selector or anything after the family head is NOT a family command (the model
# is running one test file on purpose) — see family_of.
_FAMILY = (
    ("test", re.compile(r"^(?:python3? -m )?pytest(?: -[a-zA-Z]+)*$")),
    ("test", re.compile(r"^(?:npm|yarn|pnpm) (?:run )?test$")),
    ("test", re.compile(r"^mvn(?: -[a-zA-Z]+)* (?:test|verify)$")),
    ("test", re.compile(r"^go test(?: \./\.\.\.)?$")),
    ("test", re.compile(r"^cargo test$")),
    ("test", re.compile(r"^make test$")),
    ("test", re.compile(r"^(?:\./)?gradlew? test$")),
    ("build", re.compile(r"^mvn(?: -[a-zA-Z]+)*(?: clean)? (?:compile|package|install)(?: -DskipTests)?$")),
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
_POLITE = re.compile(r"^(?:please|pls|now|ok|okay|and|then|just|go ahead and)\s+", re.I)
_SAVE_RE = re.compile(
    r"^(?:remember:?|always|from now on|recipe:?)?\s*"
    r"(?:run |do )?(?P<verb>[a-z][a-z0-9-]{0,30})\s+"
    r"(?:recipe\s+)?(?:=|is|means|with|using|via)\s+"
    r"`?(?P<cmd>[^`\n]{2,300}?)`?\s*[.!]?$", re.I)
_REMOVE_RE = re.compile(
    r"^(?:forget|remove|drop|delete)\s+(?:the\s+)?(?:recipe\s+)?(?P<verb>[a-z][a-z0-9-]{0,30})"
    r"(?:\s+recipe)?\s*[.!]?$", re.I)
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


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
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
        if isinstance(row, dict) and str(row.get("cmd") or "").strip():
            out[str(verb)] = {"cmd": str(row["cmd"]).strip(),
                              "cwd": str(row.get("cwd") or "."),
                              "aliases": [str(a) for a in (row.get("aliases") or []) if str(a).strip()],
                              "saved": row.get("saved")}
    return out


def get(root: str, verb: str) -> dict | None:
    return load(root).get(_slug(verb))


def save(root: str, verb: str, cmd: str, *, cwd: str = ".", aliases=None) -> dict:
    data = _read(_path(root))
    row = {"cmd": cmd.strip(), "cwd": cwd or ".", "saved": int(time.time())}
    if aliases:
        row["aliases"] = [str(a).strip().lower() for a in aliases if str(a).strip()]
    data[_slug(verb)] = row
    _write(_path(root), data)
    return row


def remove(root: str, verb: str) -> bool:
    data = _read(_path(root))
    if _slug(verb) not in data:
        return False
    del data[_slug(verb)]
    _write(_path(root), data)
    return True


def decline(root: str, verb: str) -> None:
    p = os.path.join(_dir(root), _DECLINED)
    data = _read(p)
    data[_slug(verb)] = int(time.time())
    _write(p, data)


def _declined(root: str) -> set:
    return set(_read(os.path.join(_dir(root), _DECLINED)))


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
    """The verb a bare message asks for, when a recipe for it exists."""
    recs = load(root)
    if not recs:
        return None
    t = _norm(text).lower().strip(" .!")
    t = _POLITE.sub("", t)
    if not t or "?" in t or len(t) > 40:
        return None
    for verb, rec in recs.items():
        phrases = set(_PHRASES.get(verb, ())) | set(rec["aliases"])
        phrases |= {verb, f"run {verb}", f"run the {verb}", verb.replace("-", " ")}
        if t in phrases:
            return verb
    return None


def parse_sentence(text: str) -> tuple[str, str, str] | None:
    """("save", verb, cmd) / ("remove", verb, "") for a one-line recipe sentence."""
    t = _norm(text)
    if not t or "\n" in text.strip() or len(t) > 320:
        return None
    m = _REMOVE_RE.match(t)
    if m:
        return "remove", _canon(m.group("verb")), ""
    m = _SAVE_RE.match(t)
    if not m:
        return None
    verb, cmd = _canon(m.group("verb")), m.group("cmd").strip()
    if verb in ("that", "this", "the", "it") or not _CMD_START.match(cmd):
        return None
    if " is what " in t or " says" in t or verb in ("remember", "always"):
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
        cmd = _norm((s.get("args") or {}).get("cmd") or (s.get("args") or {}).get("command"))
        verb = family_of(cmd)
        if not verb or verb in have or verb in declined or verb in seen:
            continue
        seen.add(verb)
        out.append({"verb": verb, "cmd": cmd, "cwd": "."})
        if len(out) == 3:
            break
    return out


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
    lines = ["RECIPES — this project's fixed commands. For these verbs run the "
             "recipe command exactly (the harness replaces a raw command with it):"]
    verbs = list(recs)
    for verb in verbs[:_MAX_BLOCK_LINES]:
        lines.append(f"- {verb} → {recs[verb]['cmd']}")
    if len(verbs) > _MAX_BLOCK_LINES:
        lines.append("more: " + ", ".join(verbs[_MAX_BLOCK_LINES:]))
    return "\n".join(lines)
