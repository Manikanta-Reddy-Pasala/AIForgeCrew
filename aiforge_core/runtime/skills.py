"""Skill registry — ``SKILL.md`` standard + searchable, self-authored.

Closes two gaps vs Hermes Agent / OpenClaw:

  1. **Standard + registry.** Adopts the agentskills.io / ``SKILL.md``
     convention: a skill is a directory containing ``SKILL.md`` with YAML
     frontmatter (``name``, ``description``, optional ``triggers`` /
     ``when_to_use`` / ``always``) and a markdown body of instructions.
     Loaded from a global dir + per-repo dirs, then **searched by
     relevance** (description + trigger overlap), not just trigger
     substring — so the agent auto-discovers the right skill.

  2. **Self-improving.** :func:`write_skill` lets an agent author a
     reusable skill the moment it solves something hard (the Hermes
     model). Authored skills land in the registry and resurface on the
     next relevant request — the learning loop.

Roots (all merged; repo-local overrides global by ``name``):
    $AIFORGE_SKILLS_DIR or ~/.aiforge/skills/<name>/SKILL.md   (global)
    <repo>/.aiforge/skills/<name>/SKILL.md
    <repo>/.claude/skills/<name>/SKILL.md
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple
from aiforge_core.config.paths import config_dir

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None  # type: ignore

# `[ \t]*` not `\s*`: `\s` MATCHES the newline, so `---\s*\n` could split a
# run of blank lines many ways — the super-linear case. What is actually
# meant is "trailing spaces/tabs on the --- line".
_FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)$", re.DOTALL)
_REPO_SUBDIRS = (".aiforge/skills", ".claude/skills")
# Per-skill body budget in the injected block. A skill that carries steps PLUS
# a strict output format easily exceeds a small cap — and truncating mid-body
# silently drops the format section the user needs honoured. Generous default,
# env-tunable for a very large registry.
try:
    _MAX_BODY = max(400, int(os.environ.get("AIFORGE_SKILL_MAX_BODY", "4000")))
except ValueError:
    _MAX_BODY = 4000
_WORD_RE = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    triggers: tuple[str, ...]
    body: str
    source: str = ""
    always: bool = False
    priority: int = 0


class Selection(NamedTuple):
    """Return contract for :func:`select_or_ask` — self-documenting in
    place of a bare ``tuple[list[Skill], list[list[Skill]]]``. Still
    unpacks positionally (``chosen, ambiguous = select_or_ask(...)``)."""
    chosen: list[Skill]
    ambiguous_groups: list[list[Skill]]


def _global_dir() -> Path:
    raw = os.environ.get("AIFORGE_SKILLS_DIR")
    if raw:
        return Path(raw).expanduser()
    # Live under the same config dir as the rest of the app (AIFORGE_CONFIG_DIR)
    # — a raw Path.home() diverges from the operator's configured/mounted dir on
    # docker/hybrid, so skills built via chat landed outside it (looked lost).
    cfg = str(config_dir())
    return Path(cfg) / "skills"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "skill").lower()).strip("-") or "skill"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a SKILL.md into its parsed YAML front-matter dict and its body. No
    front-matter (or unparseable YAML) → ({}, whole text)."""
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        return {}, text or ""
    meta: dict = {}
    if yaml is not None:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except Exception:  # noqa: BLE001
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, m.group(2).strip()


def _skill_triggers(meta: dict) -> tuple:
    """Lowercased trigger phrases from ``triggers`` / ``when_to_use`` (a list or
    a comma string)."""
    raw = meta.get("triggers") or meta.get("when_to_use") or []
    if isinstance(raw, str):
        raw = [t.strip() for t in raw.split(",")]
    return tuple(str(t).lower() for t in raw if isinstance(t, str) and t.strip())


def _parse_skill_md(text: str, default_name: str) -> Skill | None:
    meta, body = _split_frontmatter(text)
    name = str(meta.get("name") or default_name).strip()
    if not name or not body:
        return None
    always = str(meta.get("always", "")).lower() in ("true", "yes", "1") \
        or str(meta.get("type", "")).lower() == "repo"
    try:
        priority = int(meta.get("priority", 0))
    except (TypeError, ValueError):
        priority = 0
    return Skill(name=name,
                 description=str(meta.get("description") or "").strip(),
                 triggers=_skill_triggers(meta), body=body, source="",
                 always=always, priority=priority)


def _skill_md_path(child: Path) -> "Path | None":
    """The SKILL markdown for one directory entry: ``<dir>/SKILL.md`` (dir form)
    or a flat ``*.md`` file. None for anything else."""
    if child.is_dir():
        cand = child / "SKILL.md"
        return cand if cand.is_file() else None
    return child if child.suffix == ".md" else None


def _load_skill_file(md: Path, default_name: str) -> "Skill | None":
    """Parse one SKILL markdown file into a Skill, or None on any error."""
    try:
        sk = _parse_skill_md(md.read_text(encoding="utf-8", errors="ignore"),
                             default_name=default_name)
    except Exception:  # noqa: BLE001
        return None
    if sk is None:
        return None
    return Skill(**{**sk.__dict__, "source": str(md)})


def _scan_dir(root: Path) -> list[Skill]:
    """Read ``<root>/<name>/SKILL.md`` (dir form) AND ``<root>/*.md`` (flat)."""
    out: list[Skill] = []
    if not root.exists():
        return out
    try:
        for child in sorted(root.iterdir()):
            md = _skill_md_path(child)
            if md is None:
                continue
            sk = _load_skill_file(md, child.stem)
            if sk is not None:
                out.append(sk)
    except Exception:  # noqa: BLE001
        return out
    return out


def _repo_root(cwd: str | None) -> str | None:
    from aiforge_core.runtime import request_context
    return (request_context.get_workspace_dir() or cwd
            or request_context.get_repo_root() or None)


def _repo_name(cwd: str | None) -> str:
    # Canonical resolver (git-toplevel) so a skill's repo tag matches the key
    # chat/recall use — was repo_root basename, a third divergent base.
    from aiforge_core.runtime import repo_ident as _ri
    return _ri.repo_name(cwd, sentinel="skills")


def _builtin_dir() -> Path:
    """Shipped default skills (lowest priority — custom always wins)."""
    return Path(__file__).resolve().parent / "builtin_playbooks" / "skills"


def load(cwd: str | None = None) -> list[Skill]:
    """Skills, de-duped by name. Priority order (later wins on name conflict):
    BUILT-IN defaults → global user skills → repo-local. So a CUSTOM skill always
    overrides a shipped default of the same name, and ranks above it."""
    by_name: dict[str, Skill] = {}
    # 1. built-in defaults — lowest priority (custom overrides by name + outranks).
    for sk in _scan_dir(_builtin_dir()):
        by_name[sk.name] = replace(sk, source="builtin", priority=sk.priority - 100)
    # 2. global user (custom).
    for sk in _scan_dir(_global_dir()):
        by_name[sk.name] = sk
    root = _repo_root(cwd)
    if root:
        for sub in _REPO_SUBDIRS:
            for sk in _scan_dir(Path(root) / sub):
                by_name[sk.name] = sk
    return list(by_name.values())


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def _fuzzy_overlap(qtok: set[str], stok: set[str]) -> float:
    """Token overlap tolerant of inflections and typos — an exact token is
    worth 1.0; otherwise the best stem hit (``deploy``≈``deployment``, one
    side a prefix of the other, len ≥ 4) or a close difflib match (ratio ≥
    0.8, catches ``comit``≈``commit``) scores fractionally. This is what lets
    a QUESTION that doesn't use a playbook's exact trigger words still find
    it."""
    import difflib
    score = 0.0
    for q in qtok:
        if q in stok:
            score += 1.0
            continue
        best = 0.0
        for s in stok:
            if len(q) >= 4 and len(s) >= 4 and (s.startswith(q) or q.startswith(s)):
                best = max(best, 0.9)
            elif len(q) >= 4 and abs(len(q) - len(s)) <= 3:
                r = difflib.SequenceMatcher(None, q, s).ratio()
                if r >= 0.8:
                    best = max(best, r)
        score += best
    return score


def search(query: str, cwd: str | None = None, k: int = 5,
           skills: list[Skill] | None = None) -> list[dict]:
    """Relevance-rank skills for ``query``. Score = exact-trigger boost +
    token overlap with name/description/triggers. Returns
    ``[{name, description, score, source}]`` top-k (score > 0)."""
    pool = skills if skills is not None else load(cwd)
    q = (query or "").lower()
    qtok = _tokens(query)
    scored: list[tuple[float, float, Skill]] = []
    for sk in pool:
        score = 0.0
        for t in sk.triggers:
            if t and t in q:
                score += 5.0                       # exact trigger hit
        score += _fuzzy_overlap(qtok, _tokens(sk.name + " " + sk.description
                                              + " " + " ".join(sk.triggers)))
        if sk.always:
            score += 0.5
        if score > 0:
            # ``score`` is the RELEVANCE and is what we report — it is > 0 here,
            # honouring the documented "top-k (score > 0)" contract. ``priority``
            # is only a tiebreaker for equal relevance; it must NOT be folded
            # into the reported score. Built-ins carry a large negative priority
            # sentinel (-100, set in ``load()`` so a same-named custom skill
            # wins de-dup); baking priority*0.1 into the score let that sentinel
            # swamp relevance and return NEGATIVE-scored builtin noise hits.
            scored.append((score, float(sk.priority), sk))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [{"name": s.name, "description": s.description,
             "score": round(sc, 2), "source": s.source}
            for sc, _prio, s in scored[:k]]


def render(skills: list[Skill]) -> str:
    if not skills:
        return ""
    parts = []
    for sk in skills:
        head = f"### {sk.name}" + (f" — {sk.description}" if sk.description else "")
        parts.append(f"{head}\n{sk.body[:_MAX_BODY]}")
    # Directive framing: when a skill's task matches, its steps AND any output
    # format it specifies are REQUIREMENTS, followed exactly — not optional
    # suggestions. A user who wrote "present tickets EXACTLY like this" means it.
    return ("APPLICABLE SKILLS — when a task below matches the request, follow "
            "that skill's steps and reproduce ANY output format it specifies "
            "EXACTLY (verbatim structure, headers, delimiters, and wording it "
            "dictates — include every opening AND closing marker). Do not "
            "paraphrase, do not add or omit anything it forbids, and when a "
            "skill prescribes the exact output, produce it DIRECTLY — do not "
            "ask a clarifying question or add preamble first:\n"
            + "\n\n".join(parts))


def _always_on(pool: list[Skill]) -> list[Skill]:
    """Always-on skills from ``pool``, priority-ordered and capped by
    AIFORGE_SKILLS_ALWAYS_CAP (default 8) — shared by select() and
    select_or_ask() so a large registry can't blow the context budget."""
    try:
        cap = int(os.environ.get("AIFORGE_SKILLS_ALWAYS_CAP", "8"))
    except (TypeError, ValueError):
        cap = 8   # a bad env value must not raise into the shared scorer
    return sorted((s for s in pool if s.always), key=lambda s: -s.priority)[:cap]


def select(query: str, cwd: str | None = None, k: int = 4) -> list[Skill]:
    """The skills that :func:`auto_context` would inject for ``query``:
    all always-on skills (capped) + the top-``k`` relevant ones, ordered by
    priority. Factored out so callers can both render the block AND report
    *which* skills fired (workflow-transparency)."""
    pool = load(cwd)
    always_on = _always_on(pool)
    chosen: dict[str, Skill] = {s.name: s for s in always_on}
    for hit in search(query, cwd, k=k, skills=pool):
        sk = next((s for s in pool if s.name == hit["name"]), None)
        if sk is not None:
            chosen[sk.name] = sk
    return sorted(chosen.values(), key=lambda s: -s.priority)


def _ambiguity_margin() -> float:
    """Fractional gap under which candidate[1] is considered a near-tie
    with candidate[0] (0.15 = within 15% of the top score). 0 disables
    ambiguity detection entirely (old silent-pick behavior).
    Tunable via AIFORGE_AMBIGUITY_MARGIN (default 0.15)."""
    try:
        return max(0.0, float(os.environ.get("AIFORGE_AMBIGUITY_MARGIN", "0.15")))
    except (TypeError, ValueError):
        return 0.15


def _ambiguity_floor() -> float:
    """Minimum top score before a near-tie counts as real ambiguity — stops
    two near-zero garbage matches from falsely tying.
    Tunable via AIFORGE_AMBIGUITY_FLOOR (default 2.0)."""
    try:
        return max(0.0, float(os.environ.get("AIFORGE_AMBIGUITY_FLOOR", "2.0")))
    except (TypeError, ValueError):
        return 2.0


def select_or_ask(query: str, cwd: str | None = None, k: int = 4,
                  pool: list[Skill] | None = None,
                  ) -> Selection:
    """Like :func:`select` but separates out AMBIGUOUS near-ties instead of
    silently auto-picking one. Returns a :class:`Selection` (still unpacks
    as ``chosen, ambiguous_groups = select_or_ask(...)``):

    - ``chosen`` — always-on items + top relevant matches ABOVE the noise
      floor (see :func:`_ambiguity_floor`) — similar to :func:`select` but
      floor-gated, so a weak single match ``select()`` would admit is
      dropped here. When a near-tie is detected, ONE best-guess (highest
      priority, ties broken by score) from that tie is still included here
      — a caller that can't block (an autonomous run) still gets a usable
      pick.
    - ``ambiguous_groups`` — each entry is a list of 2+ Skills that scored
      within :func:`_ambiguity_margin` of each other and needed a
      best-guess instead of a confident pick. A caller that CAN ask a user
      (a live chat turn, an interactive ticket) surfaces this for
      disambiguation.

    ``always``-on items always bypass this — they are never ambiguous."""
    src_pool = pool if pool is not None else load(cwd)
    always_on = _always_on(src_pool)
    always_names = {s.name for s in always_on}
    chosen: dict[str, Skill] = {s.name: s for s in always_on}
    ambiguous: list[list[Skill]] = []
    margin = _ambiguity_margin()
    floor = _ambiguity_floor()
    # Always-on items are unconditionally included regardless of score —
    # exclude them from consideration entirely, otherwise a high-scoring
    # always-on skill can falsely "tie" with an unrelated match (there's
    # nothing to disambiguate: the always-on one applies no matter what).
    # The floor filter applies to EVERY candidate, not just the ambiguity
    # check — a weak, barely-nonzero token-overlap match (e.g. sharing only
    # a common word like "to") must not leak into the final selection just
    # because the pool happens to be small enough that top-k includes it.
    hits = [h for h in search(query, cwd, k=max(k, 4), skills=src_pool)
           if h["name"] not in always_names and h["score"] >= floor]
    if not hits:
        return Selection(sorted(chosen.values(), key=lambda s: -s.priority), ambiguous)
    top_score = hits[0]["score"]
    if (margin > 0 and len(hits) > 1
            and hits[1]["score"] >= top_score * (1 - margin)):
        near = [h for h in hits if h["score"] >= top_score * (1 - margin)]
        near_names = {h["name"] for h in near}
        group = [s for s in src_pool if s.name in near_names]
        if len(group) > 1:
            ambiguous.append(group)
            score_by_name = {h["name"]: h["score"] for h in near}
            best = sorted(
                group, key=lambda s: (-s.priority, -score_by_name[s.name]))[0]
            chosen[best.name] = best
            hits = hits[len(near):]   # remaining non-ambiguous hits below
    for h in hits[:k]:
        sk_hit = next((s for s in src_pool if s.name == h["name"]), None)
        if sk_hit is not None:
            chosen[sk_hit.name] = sk_hit
    return Selection(sorted(chosen.values(), key=lambda s: -s.priority), ambiguous)


def selected_names(query: str, cwd: str | None = None, k: int = 4) -> list[dict]:
    """``[{name, why}]`` for the skills :func:`auto_context` injects — ``why``
    is ``always`` (always-on) or ``match`` (relevance hit). Drives the
    Workflow UI's "skills used" badge."""
    always = {s.name for s in load(cwd) if s.always}
    return [{"name": s.name,
             "why": "always" if s.name in always else "match"}
            for s in select(query, cwd, k)]


def auto_context(query: str, cwd: str | None = None, k: int = 4) -> str:
    """Injection block: all always-on skills + the top-``k`` relevant ones
    for ``query`` (by :func:`search`). Empty when none apply."""
    return render(select(query, cwd, k))


def _skill_frontmatter(name: str, description: str, trig: list[str],
                       scope: str) -> str:
    """Render the OKF v0.1 SKILL.md front-matter block. ``type:`` is the one
    required field; ``name`` doubles as the OKF title; triggers/scope are
    preserved custom keys."""
    import json as _json
    front = "---\ntype: skill\n"
    front += "name: " + _json.dumps(name) + "\n"
    if description:
        front += "description: " + _json.dumps(description.strip()) + "\n"
    if trig:
        front += "triggers: [" + ", ".join(_json.dumps(t) for t in trig) + "]\n"
    front += "scope: " + _json.dumps((scope or "global").lower()) + "\n"
    front += "---\n"
    return front


def _record_skill_memory(name: str, description: str, body: str,
                         triggers: list[str] | None, scope: str,
                         cwd: str | None) -> bool:
    """Mirror the skill into knowledge memory so unified_query / the graph
    surface it alongside facts. Best-effort — the SKILL.md is the executable
    playbook; this entry just makes it retrievable cross-source."""
    try:
        from aiforge_core.runtime.tools.memory_write import memory_write as _mw
        res = _mw(
            text=f"SKILL: {name} — {description}".strip(" —")
                 + (f"\n{body[:600]}" if body else ""),
            kind="skill",
            tags=["skill", scope]
                 + ([t.strip().lower() for t in (triggers or [])][:5]),
            decision=False, repo=_repo_name(cwd))
        return bool(isinstance(res, dict) and res.get("ok", True))
    except Exception:  # noqa: BLE001
        return False


def write_skill(name: str, description: str, body: str,
                triggers: list[str] | None = None, *,
                cwd: str | None = None, scope: str = "global") -> dict:
    """Author/overwrite a reusable ``SKILL.md`` (self-improvement loop).

    ``scope`` = ``global`` (~/.aiforge/skills) or ``repo`` (<repo>/.aiforge/
    skills). Returns ``{ok, name, path}`` or ``{ok: False, error}``."""
    name = (name or "").strip()
    body = (body or "").strip()
    if not name or not body:
        return {"ok": False, "error": "name and body are required"}
    if scope == "repo":
        root = _repo_root(cwd)
        base = Path(root) / ".aiforge" / "skills" if root else _global_dir()
    else:
        base = _global_dir()
    skill_dir = base / _slug(name)
    trig = [t.strip().lower() for t in (triggers or []) if str(t).strip()]
    front = _skill_frontmatter(name, description, trig, scope)
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / "SKILL.md"
        path.write_text(front + "\n" + body + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    mem = _record_skill_memory(name, description, body, triggers, scope, cwd)
    return {"ok": True, "name": name, "path": str(path), "memory": mem}


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


def _deletable_roots(cwd: str | None) -> list[Path]:
    """Dirs a skill file may be unlinked from: global, the shipped builtin dir,
    and repo-local playbook dirs. Bounds delete so it can never remove an
    arbitrary file outside the playbook tree."""
    roots = [_global_dir(), _builtin_dir()]
    root = _repo_root(cwd)
    if root:
        roots += [Path(root) / sub for sub in _REPO_SUBDIRS]
    return roots


def _unlink_skill_file(src: str, roots) -> "str | None":
    """Unlink one skill's backing file if it lives under a deletable root; also
    drop a now-empty ``<name>/`` dir left by the SKILL.md form. Returns the path
    removed, or None (synthetic/out-of-bounds/already gone). Raises OSError on a
    real unlink failure the caller surfaces."""
    if not src or src == "builtin":
        return None                # no on-disk path (already gone / synthetic)
    p = Path(src)
    if not any(_within(p, r) for r in roots):
        return None
    try:
        p.unlink()
    except FileNotFoundError:
        return None
    if p.name == "SKILL.md" and p.parent.is_dir() and not any(p.parent.iterdir()):
        p.parent.rmdir()
    return str(p)


def delete_skill(name: str, cwd: str | None = None) -> dict:
    """Delete the skill(s) named ``name`` by unlinking the backing file (custom
    OR shipped default). Bounded to the playbook dirs. Returns
    ``{ok, removed:[paths]}`` or ``{ok: False, error}``."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    roots = _deletable_roots(cwd)
    removed: list[str] = []
    for sk in load(cwd):
        if sk.name != name:
            continue
        try:
            got = _unlink_skill_file(getattr(sk, "source", ""), roots)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if got:
            removed.append(got)
    if not removed:
        return {"ok": False, "error": f"no deletable skill named {name!r}"}
    return {"ok": True, "name": name, "removed": removed}


def clear_skills(cwd: str | None = None) -> dict:
    """Delete every deletable skill (custom + defaults). Returns count removed."""
    names = {s.name for s in load(cwd)}
    removed = 0
    for n in names:
        r = delete_skill(n, cwd)
        if r.get("ok"):
            removed += len(r.get("removed", []))
    return {"ok": True, "removed": removed}


__all__ = ["Skill", "Selection", "load", "search", "render", "select",
           "select_or_ask", "selected_names", "auto_context", "write_skill",
           "delete_skill", "clear_skills"]
