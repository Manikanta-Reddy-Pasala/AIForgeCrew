"""Glob-scoped repo rules — Cursor-style, deterministic, zero LLM cost.

Cursor loads ``.cursor/rules/*`` scoped by file globs so only the rules
matching the files being touched reach the prompt. We adopt the same:
rules live IN THE TARGET REPO and are matched against the ticket's
scope globs, replacing a paid ``ctx_conventions`` LLM gathering call
whenever the repo actually carries rules files.

Sources scanned (in the per-ticket worktree), first match per name wins:

* ``.aiforge/rules/*.md``   — our native location
* ``.cursor/rules/*.mdc|*.md`` — Cursor project rules (frontmatter:
  ``description`` / ``globs`` / ``alwaysApply``)
* ``.cursorrules``          — legacy single-file Cursor rules (always)
* ``AGENTS.md``             — agent instructions convention (always)

Matching (v1, deterministic): a rule applies when

* it has ``alwaysApply: true`` or declares no globs, or
* any of its globs fnmatch-intersects any ticket scope glob — checked in
  BOTH directions so ``src/**`` (rule) matches ``src/a/**`` (scope) and
  vice versa.

Rendered as one capped markdown block → ``state['rules_md']`` →
``{rules_md?}`` in the planner + doer prompts.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger("aiforge.repo_rules")

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
# Hard cap on the rendered block — rules are prompt overhead on every
# planner/doer turn; a repo with sprawling rules must not eat the budget.
_MAX_CHARS = int(os.environ.get("AIFORGE_RULES_MAX_CHARS", "6000"))


@dataclass(frozen=True)
class Rule:
    name: str
    globs: tuple[str, ...]      # empty = always applies
    always: bool
    body: str
    source: str


def _parse_rule_file(path: Path) -> Rule | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta: dict = {}
    body = text.strip()
    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        body = m.group(2).strip()
    if not body:
        return None
    raw_globs = meta.get("globs") or []
    if isinstance(raw_globs, str):
        raw_globs = [g.strip() for g in raw_globs.split(",") if g.strip()]
    globs = tuple(str(g) for g in raw_globs if g)
    always = bool(meta.get("alwaysApply", False))
    return Rule(
        name=str(meta.get("description") or path.stem),
        globs=globs, always=always, body=body, source=str(path),
    )


def _global_rules_dir() -> Path:
    base = os.environ.get("AIFORGE_RULES_DIR")
    if base:
        return Path(base).expanduser()
    cfg = os.environ.get("AIFORGE_CONFIG_DIR", os.path.expanduser("~/.aiforge"))
    return Path(cfg).expanduser() / "rules"


def load_global_rules() -> list[Rule]:
    """Operator-authored rules in ~/.aiforge/rules/*.md (UI-managed)."""
    rules: list[Rule] = []
    d = _global_rules_dir()
    if d.is_dir():
        for path in sorted(d.glob("*.md")):
            r = _parse_rule_file(path)
            if r is not None:
                rules.append(r)
    return rules


def write_rule(name: str, body: str, *, globs: list[str] | None = None,
               always: bool = True) -> dict:
    """Author/overwrite a global rule at ~/.aiforge/rules/<slug>.md with Cursor-
    style frontmatter. Returns ``{ok, name, path}`` or ``{ok: False, error}``."""
    name = (name or "").strip()
    body = (body or "").strip()
    if not name or not body:
        return {"ok": False, "error": "name and body are required"}
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "rule"
    d = _global_rules_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        gl = [g.strip() for g in (globs or []) if str(g).strip()]
        front = ["---", f"name: {name}", f"alwaysApply: {str(bool(always)).lower()}"]
        if gl:
            front.append("globs: " + ", ".join(gl))
        front.append("---")
        path = d / f"{slug}.md"
        path.write_text("\n".join(front) + "\n\n" + body + "\n", encoding="utf-8")
        return {"ok": True, "name": name, "path": str(path)}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def load_rules(repo_root: str | Path) -> list[Rule]:
    """Scan the repo's rule sources + the operator's global rules
    (~/.aiforge/rules, UI-managed). Soft — missing dirs return []."""
    root = Path(repo_root)
    rules: list[Rule] = list(load_global_rules())   # operator global rules first
    for pattern in (".aiforge/rules/*.md", ".cursor/rules/*.mdc",
                    ".cursor/rules/*.md"):
        for path in sorted(root.glob(pattern)):
            r = _parse_rule_file(path)
            if r is not None:
                rules.append(r)
    # Always-on single files (no frontmatter expected).
    for name in (".cursorrules", "AGENTS.md"):
        path = root / name
        if path.is_file():
            try:
                body = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if body:
                rules.append(Rule(name=name, globs=(), always=True,
                                  body=body, source=str(path)))
    return rules


def _split_glob(g: str) -> tuple[str, str]:
    """Split a glob into (dir-prefix, basename-pattern).

    ``aiforge_core/**/*.py`` → ("aiforge_core", "*.py");
    ``src/a/**`` → ("src/a", "**"); ``*.py`` → ("", "*.py").
    The dir-prefix is the longest leading run of literal path segments.
    """
    parts = g.split("/")
    prefix: list[str] = []
    for seg in parts[:-1]:
        if any(ch in seg for ch in "*?["):
            break
        prefix.append(seg)
    base = parts[-1] if parts else g
    return "/".join(prefix), base or "**"


def _globs_intersect(rule_glob: str, scope_glob: str) -> bool:
    """Pattern-vs-pattern intersection.

    v1 fnmatch-both-ways failed the DOMINANT real combination —
    extension rule globs (Cursor ``.mdc`` canon: ``**/*.py``) vs
    directory scope globs (ticket canon: ``src/a/**``) — silently
    making most glob-scoped rules inert. v2 splits each glob into
    (dir-prefix, basename-pattern): they intersect when one prefix is a
    path-prefix of the other (or fnmatch-compatible) AND either
    basename is a wildcard or the basenames fnmatch each other.
    """
    rp, rb = _split_glob(rule_glob)
    sp, sb = _split_glob(scope_glob)
    # dir-prefix compatibility: one literal prefix extends the other,
    # or the shorter pattern's prefix fnmatches into the longer's.
    prefix_ok = (
        rp == sp
        or rp.startswith(sp + "/") or sp.startswith(rp + "/")
        or rp == "" or sp == ""
        or fnmatch.fnmatch(sp, rp) or fnmatch.fnmatch(rp, sp)
    )
    if not prefix_ok:
        return False
    wild = ("*", "**")
    if rb in wild or sb in wild:
        return True
    return fnmatch.fnmatch(sb, rb) or fnmatch.fnmatch(rb, sb)


def match_rules(rules: list[Rule], scope_globs: list[str] | None) -> list[Rule]:
    """Rules that apply to this ticket's scope."""
    out: list[Rule] = []
    for r in rules:
        if r.always or not r.globs:
            out.append(r)
            continue
        for sg in (scope_globs or []):
            if any(_globs_intersect(rg, sg) for rg in r.globs):
                out.append(r)
                break
    return out


def render(rules: list[Rule]) -> str:
    """One capped markdown block for ``state['rules_md']``."""
    if not rules:
        return ""
    parts: list[str] = []
    used = 0
    for r in rules:
        block = f"### {r.name}\n{r.body}"
        if used + len(block) > _MAX_CHARS:
            remaining = _MAX_CHARS - used
            if remaining > 200:
                parts.append(block[:remaining] + "\n…(rule truncated)")
            log.info("repo_rules: cap hit — %d rule(s) truncated/dropped",
                     len(rules) - len(parts))
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def collect(repo_root: str | Path,
            scope_globs: list[str] | None = None) -> str:
    """Load + match + render in one call. '' when the repo has no rules."""
    try:
        rules = load_rules(repo_root)
        if not rules:
            return ""
        return render(match_rules(rules, scope_globs))
    except Exception as exc:  # noqa: BLE001 — rules must never block a run
        log.debug("repo_rules.collect failed: %s", exc)
        return ""


def matched_names(repo_root: str | Path,
                  scope_globs: list[str] | None = None) -> list[dict]:
    """``[{name, source}]`` for the rules that apply to this scope — so the
    Workflow UI can show which rules a run actually pulled in. Never raises."""
    try:
        rules = load_rules(repo_root)
        if not rules:
            return []
        return [{"name": r.name, "source": getattr(r, "source", "") or ""}
                for r in match_rules(rules, scope_globs)]
    except Exception as exc:  # noqa: BLE001 — rules must never block a run
        log.debug("repo_rules.matched_names failed: %s", exc)
        return []


__all__ = ["Rule", "load_rules", "match_rules", "render", "collect",
           "matched_names"]
