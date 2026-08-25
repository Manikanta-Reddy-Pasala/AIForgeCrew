from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .._shell import _workspace_root
from ._shared import _chat_repo_key, _coerce_int, _elaborate_body


def _t_memory_lookup(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.memory import unified_query as _uq
        # F2/M3: scope recall to the SAME repo the chat WRITE path files under
        # (git-toplevel basename), so chat's own facts aren't filtered out.
        _repo = _chat_repo_key(cwd)
        res = _uq.query(args["query"], limit=int(args.get("limit", 6)),
                        repo=_repo)
        return {"ok": True, "hits": [
            {"text": (h.get("text") or "")[:400], "source": h.get("source")}
            for h in res.get("hits", [])
        ]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_search_chat_sessions(args: dict, _cwd: str) -> dict:
    """Search PRIOR chat sessions' message content — recall what you discussed
    with the user in past conversations. Local + cheap (one SQLite scan)."""
    try:
        q = args.get("query") or args.get("q") or ""
        limit = _coerce_int(args.get("limit"), 6)
        from aiforge_core.runtime import chat_store
        return {"ok": True, "hits": chat_store.search_messages(q, limit=limit)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_memory_write(args: dict, cwd: str) -> dict:
    """Persist a durable fact/decision into the knowledge memory so future
    chats + tickets recall it. repo defaults to the working dir's name."""
    try:
        from aiforge_core.runtime.tools.memory_write import memory_write as _mw
        # Use the SAME git-toplevel repo key the recall path uses
        # (_chat_repo_key), so a fact written from a SUBDIRECTORY chat is
        # filed under the repo the later recall queries — otherwise a subdir
        # write lands under the subdir basename and is never recalled.
        repo = args.get("repo") or _chat_repo_key(cwd) or "chat"
        # scope="global" writes a repo-less fact recalled across EVERY ticket/
        # page/repo (general knowledge); default scope keeps it to THIS context.
        _scope = (args.get("scope") or "").lower()
        return _mw(
            text=args["text"],
            kind=args.get("kind", "note"),
            tags=list(args.get("tags") or []) + ["chat"],
            decision=bool(args.get("decision")),
            repo=repo,
            scope=_scope,
            source="chat",          # attribute the write to the chat agent
        )
    except KeyError:
        return {"ok": False, "error": "missing arg: text"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build",
              "__pycache__", ".next", "target", ".gradle", ".idea"}


def _matches_in(root: str, names, base: str, needle: str,
                suffix: str = "") -> list[str]:
    return [os.path.relpath(os.path.join(root, n), base) + suffix
            for n in names if not needle or needle in n.lower()]


def _t_find(args: dict, cwd: str) -> dict:
    """Fuzzy-locate files/dirs by partial name — so a vague/wrong folder
    name still resolves. args: name (substring, case-insensitive),
    kind ('dir'|'file'|'any'), limit."""
    base = str(_workspace_root() or cwd)
    needle = (args.get("name") or args.get("query") or "").lower()
    kind = (args.get("kind") or "any").lower()
    limit = int(args.get("limit", 60))
    hits: list[str] = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        if kind in ("dir", "any"):
            hits += _matches_in(root, dirs, base, needle, "/")
        if kind in ("file", "any"):
            hits += _matches_in(root, files, base, needle)
        if len(hits) >= limit:
            break
    return {"ok": True, "base": base, "matches": hits[:limit],
            "truncated": len(hits) > limit}


def _grep_target(base: str, want) -> tuple[str, str]:
    """``(dir_to_search, note)`` — tolerant of a wrong ``path``: fall back to
    the whole project and SAY so rather than returning nothing."""
    if not want:
        return base, ""
    cand = want if os.path.isabs(want) else os.path.join(base, want)
    if os.path.exists(cand):
        return cand, ""
    return base, f"path {want!r} not found — searched the whole project instead"


def _ripgrep(pattern: str, target: str, glob, limit: int) -> list[str] | None:
    """ripgrep's hits, or None when rg is absent or failed (caller falls back)."""
    import shutil as _sh
    rg = _sh.which("rg")
    if not rg:
        return None
    cmd = [rg, "-n", "-i", "--no-heading", "-m", str(limit)]
    for d in _SKIP_DIRS:
        cmd += ["-g", f"!{d}"]
    if glob:
        cmd += ["-g", glob]
    cmd += [pattern, target]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return (p.stdout or "").splitlines()[:limit]
    except Exception:  # noqa: BLE001
        return None


def _grep_file(fp: str, rx, base: str, out: list[str], limit: int) -> bool:
    """Append this file's hits; True when the overall limit is reached."""
    try:
        with open(fp, encoding="utf-8", errors="ignore") as fh:
            for i, ln in enumerate(fh, 1):
                if rx.search(ln):
                    out.append(f"{os.path.relpath(fp, base)}:{i}:{ln.rstrip()[:200]}")
                    if len(out) >= limit:
                        return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _python_grep(pattern: str, target: str, base: str, glob,
                 limit: int) -> tuple[list[str], bool, str]:
    """``(matches, truncated, error)`` — the dependency-free fallback."""
    import fnmatch as _fn
    import re as _re2
    try:
        rx = _re2.compile(pattern, _re2.IGNORECASE)
    except _re2.error as e:
        return [], False, f"bad regex: {e}"
    out: list[str] = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            # fnmatch handles *.py, test_*, *_spec.ts etc. (the old
            # endswith(glob.lstrip("*")) only matched suffix globs).
            if glob and not _fn.fnmatch(f, glob):
                continue
            if _grep_file(os.path.join(root, f), rx, base, out, limit):
                return out, True, ""
    return out, False, ""


def _t_grep(args: dict, cwd: str) -> dict:
    """Recursive content search (ripgrep if present, else Python). Tolerant
    of a wrong ``path``: falls back to the working dir + says so. args:
    pattern (required), path (optional), glob (optional file filter)."""
    pattern = args.get("pattern") or args.get("query") or ""
    if not pattern:
        return {"ok": False, "error": "missing 'pattern'"}
    base = str(_workspace_root() or cwd)
    target, note = _grep_target(base, args.get("path"))
    limit = int(args.get("limit", 80))
    glob = args.get("glob")
    lines = _ripgrep(pattern, target, glob, limit)
    if lines is not None:
        return {"ok": True, "matches": lines, "note": note,
                "truncated": len(lines) >= limit}
    matches, truncated, error = _python_grep(pattern, target, base, glob, limit)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "matches": matches, "note": note,
            "truncated": truncated}


def _csv_list(value):
    """A comma-separated string OR an already-parsed list, as a list."""
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def _rule_name(args: dict, text: str) -> str:
    """A stable name from an explicit arg or the first line of the text —
    repo_rules keys the file by a slug of the name."""
    name = (args.get("name") or "").strip()
    if name:
        return name
    first = text.lstrip("-# ").splitlines()[0] if text else "rule"
    return re.sub(r"\s+", " ", first).strip()[:60] or "rule"


def _record_rule_memory(text: str, scope: str, cwd: str) -> None:
    """Record the rule in knowledge memory so unified_query / recall surface it
    alongside facts. scope=repo → tag to THIS repo; scope=global →
    repo-agnostic (repo=None; recall unions NULL-repo rows so it applies
    everywhere). Writes directly to the embedded store — memory_write refuses a
    null repo, which would silently drop global rules."""
    try:
        from aiforge_core.memory import backend_select as _bsel
        if not _bsel.embedded():
            return
        from aiforge_core.memory import sqlite_memory as _sqlmem
        _sqlmem.write_unit(
            text=f"RULE: {text}", kind="note", source="rule",
            tags=["rule", scope],
            repo=(_chat_repo_key(cwd) if scope == "repo" else None))
    except Exception:  # noqa: BLE001 — memory write must not block the rule
        pass


def _t_remember_rule(args: dict, cwd: str) -> dict:
    """Persist a user rule that must apply to EVERY future session. Writes to
    the SAME global rules store the Library UI lists/creates/deletes
    (``repo_rules`` → ~/.aiforge/rules/) so a rule built in chat shows up in the
    Library — and is injected every turn by ``_rules_context``."""
    try:
        from aiforge_core.runtime import repo_rules
        text = (args.get("text") or args.get("rule") or args.get("body")
                or "").strip()
        if not text:
            return {"ok": False, "error": "missing 'text'"}
        name = _rule_name(args, text)
        description = (args.get("description") or "").strip()
        scope = (args.get("scope") or "global").lower()
        # The elaborated markdown is the RULE FILE body; the user's own
        # sentence stays in `text` so the memory unit records the statement the
        # user actually made (a whole '# Title + bullets' doc is useless as a
        # recall unit and drifts with every model rewrite).
        body = _elaborate_body("rule", text, name=name,
                               description=description)   # LLM format+elaborate
        # Unified artifact frontmatter (same shape as skills/workflows):
        # name / description / triggers / scope.
        res = repo_rules.write_rule(
            name, body, globs=_csv_list(args.get("globs")), always=True,
            description=description, triggers=_csv_list(args.get("triggers")),
            scope=scope)
        if not res.get("ok"):
            return res
        _record_rule_memory(text, scope, cwd)
        return {"ok": True, "name": name, "scope": scope, "remembered": body,
                "path": res.get("path")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


_BULLET_TRIGGERS_RE = re.compile(r"^\[triggers:[ \t]*([^\]]*)\][ \t]*(.*)$")


def _parse_bullet(line: str) -> tuple[tuple[str, ...], str]:
    """Strip a leading '- ' and an optional '[triggers: a, b]' prefix.
    Returns (triggers, text). No triggers prefix → triggers=() (always-on,
    backward compatible with every bullet written before this feature)."""
    text = line[2:] if line.startswith("- ") else line
    m = _BULLET_TRIGGERS_RE.match(text.strip())
    if not m:
        return (), text.strip()
    trig = tuple(t.strip().lower() for t in m.group(1).split(",") if t.strip())
    return trig, m.group(2).strip()


# `md_store._find_by_source` globs + parses EVERY file in the memory dir
# until it finds a frontmatter `source` match — called 2-3x per chat turn
# (`_rules_context` x2 + `_repo_context`), so it re-scans the whole memory
# dir on every single message with no caching at all. Cache POSITIVE hits
# only (never negative — a source with no file yet must keep being
# re-checked, since capture can create it moments later in the same turn);
# once a source's file exists its identity never changes (bullets are
# appended into the same file), so caching the path is always safe once
# found. `.exists()` on a hit is a cheap stat, far cheaper than the O(files)
# scan it replaces. Keyed by (memory_dir, source) — NOT source alone — so a
# changed AIFORGE_MEMORY_MD_DIR (tests reconfigure it per-case; a real
# deployment could reconfigure it too) can never serve a stale path from a
# now-irrelevant memory directory.
_source_path_cache: dict[tuple[str, str], Path] = {}


def _cached_find_by_source(source: str) -> Path | None:
    from aiforge_core.memory import md_store
    key = (str(md_store.memory_dir()), source)
    p = _source_path_cache.get(key)
    if p is not None:
        try:
            if p.exists():
                return p
        except Exception:  # noqa: BLE001 — a bad cache entry is a miss, not a crash
            pass
    p = md_store._find_by_source(source)
    if p is not None:
        _source_path_cache[key] = p
    return p


def _is_pref_row(row) -> bool:
    """A preference unit carries a ``pref:``-prefixed tag."""
    import json as _json
    try:
        tags = _json.loads(row["tags"] or "[]") or []
    except (TypeError, ValueError):
        return False
    return any(isinstance(t, str) and t.startswith("pref:") for t in tags)


def _preference_lines() -> list[str]:
    from aiforge_core.memory import sqlite_memory as _m
    lines: list[str] = []
    with _m._conn() as c:  # noqa: SLF001 — internal read, best-effort
        rows = c.execute(
            "SELECT text, tags FROM memory_units WHERE kind='preference' "
            "ORDER BY id DESC LIMIT 40").fetchall()
    for r in rows:
        txt = (r["text"] or "").strip().replace("\n", " ")
        if txt and _is_pref_row(r):
            lines.append(f"- {txt[:240]}")
    return lines


def _preferences_context(_cwd: str) -> str:
    """The user's captured PREFERENCES (global defaults/conventions), injected
    every turn so a once-stated preference is always honoured and never
    re-asked. Stored as ``pref:``-tagged units by preference_capture; embedded
    backend only. Best-effort — never breaks the turn."""
    try:
        from aiforge_core.memory import backend_select as _bsel
        if not _bsel.embedded():
            return ""
        lines = _preference_lines()
        if not lines:
            return ""
        return ("USER PREFERENCES (standing defaults/conventions the user set — "
                "apply them without asking again):\n" + "\n".join(lines[:40]))
    except Exception:  # noqa: BLE001
        return ""


def _md_rule_bullets(cwd: str) -> tuple[list[str], list]:
    """``(always_lines, tagged_skills)`` from the md rule books.

    Untagged bullets are always-on (legacy); a bullet carrying an inline
    '[triggers: ...]' prefix becomes a trigger-gated skill.

    Aligns the repo rule key with the canonical recall key (_chat_repo_key,
    git-toplevel) so a rule captured for this repo is read back under the SAME
    key it was written — _repo_name (workspace/subdir basename) drifted.
    """
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import skills as _sk
    always: list[str] = []
    tagged: list = []
    for src in ("rules:global", f"rules:{_chat_repo_key(cwd)}"):
        p = _cached_find_by_source(src)
        if p is None:
            continue
        for line in md_store._parse(p).get("body", "").splitlines():
            if not line.strip():
                continue
            trig, text = _parse_bullet(line)
            if not trig:
                always.append("- " + text)
            else:
                tagged.append(_sk.Skill(
                    name=text[:60], description="", triggers=trig,
                    body=text, source=src, always=False, priority=0))
    return always, tagged


def _repo_rule_bullets(cwd: str) -> tuple[list[str], list]:
    """The SAME store the Library UI / create-form / remember_rule write to.

    Uses load_rules(cwd), which merges builtin → global (~/.aiforge/rules) →
    repo-local and dedups BY NAME with the most-specific winning: a REPO rule
    OVERRIDES a global rule of the same name (parity with the team/pipeline
    path). An always-on rule joins the always block; a glob-scoped rule becomes
    a trigger-gated bullet so relevance scoring applies.
    """
    from aiforge_core.runtime import repo_rules as _rr
    from aiforge_core.runtime import skills as _sk
    try:
        # load_rules needs a repo root; fall back to global-only when we're not
        # in a repo (still honours global rules).
        rules = list(_rr.load_rules(cwd)) if cwd else list(_rr.load_global_rules())
    except Exception:  # noqa: BLE001
        rules = list(_rr.load_global_rules())
    always: list[str] = []
    tagged: list = []
    for r in rules:
        text = (r.body or "").strip()
        if not text:
            continue
        flat = text.replace("\n", " ")[:400]
        if getattr(r, "always", True) or not getattr(r, "globs", None):
            always.append("- " + flat)
        else:
            tagged.append(_sk.Skill(
                name=(r.name or text[:60]), description="",
                triggers=tuple(str(g).lower() for g in r.globs),
                body=flat, source="repo_rules", always=False, priority=0))
    return always, tagged


def _select_tagged(tagged: list, query: str) -> tuple[list[str], str]:
    """``(bullets, ambiguous_note)`` for the trigger-gated rules.

    The scorer runs in its OWN guard: a select_or_ask defect must never drop
    the legacy always-on bullets (the noise-fix turning into a rules-vanish
    bug). On any scorer error — or with no query to score against — fail OPEN
    and include every tagged bullet.
    """
    from aiforge_core.runtime import skills as _sk
    if not query:
        return ["- " + s.body for s in tagged], ""
    try:
        chosen, ambiguous = _sk.select_or_ask(query, pool=tagged, k=len(tagged))
    except Exception:  # noqa: BLE001
        return ["- " + s.body for s in tagged], ""
    note = ""
    if ambiguous:
        names = " or ".join(f"'{s.body}'" for s in ambiguous[0])
        note = ("\nAMBIGUOUS RULE MATCH: " + names + " both matched"
                " — ASK the user which applies before proceeding, don't guess.")
    return ["- " + s.body for s in chosen], note


def _capped_rules(blocks: list[str]) -> str:
    """Rules are MANDATORY — never silently drop tail rules to a hard slice.
    The cap is env-tunable and a truncation is called out so the agent knows
    rules exist beyond the cut (and can look them up) instead of treating the
    visible subset as complete."""
    try:
        cap = max(400, int(os.environ.get("AIFORGE_RULES_MAX_CHARS", "4000")))
    except ValueError:
        cap = 4000
    body = "\n".join(blocks)
    if len(body) <= cap:
        return body
    return (body[:cap] + "\n- …more rules truncated — call memory_lookup"
            "(\"rules\") for the full list before acting")


def _rules_context(cwd: str, query: str = "") -> str:
    """The user's persistent rule book (global + this-repo), injected into
    EVERY session so the rules are always honoured. Untagged bullets are
    always-on (legacy). Bullets tagged with an inline '[triggers: ...]'
    prefix are gated by relevance to ``query`` via the shared scorer; a
    near-tie among tagged bullets injects an ASK note instead of silently
    picking one."""
    try:
        always, tagged = _md_rule_bullets(cwd)
        try:
            repo_always, repo_tagged = _repo_rule_bullets(cwd)
        except Exception:  # noqa: BLE001 — repo_rules read is best-effort
            repo_always, repo_tagged = [], []
        always += repo_always
        tagged += repo_tagged
        selected, ambiguous_note = (_select_tagged(tagged, query) if tagged
                                    else ([], ""))
        blocks = always + selected
        if not blocks:
            return ""
        return ("RULES — MANDATORY, non-negotiable: the user ordered these "
                "ALWAYS followed, every session, HIGHEST priority (they "
                "override defaults and convenience). Check your plan against "
                "each rule before answering or acting:\n"
                + _capped_rules(blocks) + ambiguous_note)
    except Exception:  # noqa: BLE001
        return ""
