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
    triggers: tuple[str, ...] = ()   # optional topic gate (OR'd with globs)
    description: str = ""            # unified frontmatter (name/description/triggers/scope)
    scope: str = "global"


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
    raw_triggers = meta.get("triggers") or []
    if isinstance(raw_triggers, str):
        raw_triggers = [t.strip() for t in raw_triggers.split(",")]
    triggers = tuple(str(t).lower() for t in raw_triggers
                     if isinstance(t, str) and t.strip())
    # Unified frontmatter: prefer an explicit `name`; fall back to the legacy
    # convention where `description` doubled as the display label, then the
    # filename. `description` is now its own field (kept separate from name).
    name = str(meta.get("name") or meta.get("description") or path.stem)
    description = str(meta.get("description") or "")
    scope = str(meta.get("scope") or "global").lower()
    return Rule(
        name=name, globs=globs, always=always, body=body, source=str(path),
        triggers=triggers, description=description, scope=scope,
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
               always: bool = True, description: str = "",
               triggers: list[str] | None = None, scope: str = "global") -> dict:
    """Author/overwrite a global rule at ~/.aiforge/rules/<slug>.md.

    Emits the UNIFIED artifact frontmatter shared by rules, skills, and
    workflows — ``name`` / ``description`` / ``triggers`` / ``scope`` — plus the
    Cursor-compat ``alwaysApply`` / ``globs`` the deterministic scope-matcher
    still reads. Returns ``{ok, name, path}`` or ``{ok: False, error}``."""
    name = (name or "").strip()
    body = (body or "").strip()
    if not name or not body:
        return {"ok": False, "error": "name and body are required"}
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "rule"
    d = _global_rules_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        gl = [g.strip() for g in (globs or []) if str(g).strip()]
        trig = [t.strip().lower() for t in (triggers or []) if str(t).strip()]
        front = ["---", f"name: {name}"]
        if description.strip():
            front.append(f"description: {description.strip()}")
        if trig:
            front.append("triggers: [" + ", ".join(trig) + "]")
        front.append(f"scope: {(scope or 'global').lower()}")
        front.append(f"alwaysApply: {str(bool(always)).lower()}")
        if gl:
            front.append("globs: " + ", ".join(gl))
        front.append("---")
        path = d / f"{slug}.md"
        path.write_text("\n".join(front) + "\n\n" + body + "\n", encoding="utf-8")
        return {"ok": True, "name": name, "path": str(path)}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def delete_rule(name: str) -> dict:
    """Delete the global rule(s) named ``name`` by unlinking the backing file,
    bounded to the rules dirs. Returns ``{ok, removed:[paths]}``."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    roots = [_global_rules_dir().resolve(), _builtin_rules_dir().resolve()]
    removed: list[str] = []
    for r in load_global_rules():
        if r.name != name:
            continue
        src = getattr(r, "source", "")
        if not src:
            continue
        p = Path(src)
        try:
            if p.resolve().parent not in roots:
                continue
            p.unlink()
            removed.append(str(p))
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
    if not removed:
        return {"ok": False, "error": f"no deletable rule named {name!r}"}
    return {"ok": True, "name": name, "removed": removed}


def clear_rules() -> dict:
    names = {r.name for r in load_global_rules()}
    removed = 0
    for n in names:
        res = delete_rule(n)
        if res.get("ok"):
            removed += len(res.get("removed", []))
    return {"ok": True, "removed": removed}


def _builtin_rules_dir() -> Path:
    """Shipped default rules (lowest priority — a custom rule of the same name
    always wins)."""
    return Path(__file__).resolve().parent / "builtin_playbooks" / "rules"


def load_rules(repo_root: str | Path) -> list[Rule]:
    """Rules de-duped by name, precedence (later wins): BUILT-IN defaults →
    operator global (~/.aiforge/rules) → repo-local. A CUSTOM rule always
    overrides a shipped default of the same name. Soft — missing dirs → []."""
    from dataclasses import replace as _replace
    root = Path(repo_root)
    by_name: dict[str, Rule] = {}
    # 1. built-in defaults (lowest).
    bdir = _builtin_rules_dir()
    if bdir.is_dir():
        for path in sorted(bdir.glob("*.md")) + sorted(bdir.glob("*.mdc")):
            r = _parse_rule_file(path)
            if r is not None:
                by_name[r.name] = _replace(r, source="builtin")
    # 2. operator global (custom).
    for r in load_global_rules():
        by_name[r.name] = r
    # 3. repo-local (most specific).
    for pattern in (".aiforge/rules/*.md", ".cursor/rules/*.mdc",
                    ".cursor/rules/*.md"):
        for path in sorted(root.glob(pattern)):
            r = _parse_rule_file(path)
            if r is not None:
                by_name[r.name] = r
    for name in (".cursorrules", "AGENTS.md"):
        path = root / name
        if path.is_file():
            try:
                body = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if body:
                by_name[name] = Rule(name=name, globs=(), always=True,
                                     body=body, source=str(path))
    return list(by_name.values())


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


def match_rules_with_triggers(
    rules: list[Rule], scope_globs: list[str] | None, query: str,
) -> tuple[list[Rule], list[list[Rule]]]:
    """Like :func:`match_rules` but ALSO scores rules with no glob hit
    against ``query`` (the ticket title+body) via the same trigger-scorer
    skills/workflows use. Returns ``(matched, ambiguous_groups)`` — a rule
    fires if its globs match (as before) OR its triggers score a confident
    hit against ``query``; a near-tie among trigger-scored candidates is
    reported in ``ambiguous_groups`` instead of silently picked (a
    best-guess is still included in ``matched``, same contract as
    :func:`aiforge_core.runtime.skills.select_or_ask`)."""
    matched: list[Rule] = []
    trigger_pool: list[Rule] = []
    for r in rules:
        # "no globs" only means always-applies when there are ALSO no
        # triggers — a trigger-only rule (globs=() but triggers set) must
        # still go through the trigger scorer below, not be treated as
        # unconditional (that would silently bypass gating entirely).
        if r.always or (not r.globs and not r.triggers):
            matched.append(r)
            continue
        glob_hit = bool(r.globs) and any(
            _globs_intersect(rg, sg) for rg in r.globs
            for sg in (scope_globs or []))
        if glob_hit:
            matched.append(r)
        elif r.triggers:
            trigger_pool.append(r)
    ambiguous: list[list[Rule]] = []
    if trigger_pool and not query:
        # No query to score against — fail OPEN (include all trigger rules),
        # matching chat_agent._rules_context's no-query behavior. Failing
        # closed here would silently drop a trigger-only repo rule that the
        # old glob-only match_rules() would always have applied.
        matched.extend(trigger_pool)
    elif query and trigger_pool:
        from aiforge_core.runtime import skills as _sk
        pool_skills = [
            _sk.Skill(name=r.name, description="", triggers=r.triggers,
                      body=r.body, source=r.source, always=False, priority=0)
            for r in trigger_pool]
        chosen_sk, amb_sk = _sk.select_or_ask(
            query, k=len(trigger_pool), pool=pool_skills)
        by_name = {r.name: r for r in trigger_pool}
        matched.extend(by_name[s.name] for s in chosen_sk if s.name in by_name)
        for grp in amb_sk:
            ambiguous.append([by_name[s.name] for s in grp if s.name in by_name])
    return matched, ambiguous


def collect_or_ask(repo_root: str | Path, scope_globs: list[str] | None,
                   query: str) -> tuple[str, list[list[Rule]]]:
    """Like :func:`collect` but trigger-aware — rules with no glob hit are
    also scored against ``query``. Returns ``(rendered_md, ambiguous_groups)``.
    '' + [] on any error (rules must never block a run)."""
    try:
        rules = load_rules(repo_root)
        if not rules:
            return "", []
        matched, ambiguous = match_rules_with_triggers(rules, scope_globs, query)
        return render(matched), ambiguous
    except Exception as exc:  # noqa: BLE001
        log.debug("repo_rules.collect_or_ask failed: %s", exc)
        return "", []


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
           "matched_names", "match_rules_with_triggers", "collect_or_ask"]
