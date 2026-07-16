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


def _t_search_chat_sessions(args: dict, cwd: str) -> dict:
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
            for d in dirs:
                if not needle or needle in d.lower():
                    hits.append(os.path.relpath(os.path.join(root, d), base) + "/")
        if kind in ("file", "any"):
            for f in files:
                if not needle or needle in f.lower():
                    hits.append(os.path.relpath(os.path.join(root, f), base))
        if len(hits) >= limit:
            break
    return {"ok": True, "base": base, "matches": hits[:limit],
            "truncated": len(hits) > limit}


def _t_grep(args: dict, cwd: str) -> dict:
    """Recursive content search (ripgrep if present, else Python). Tolerant
    of a wrong ``path``: falls back to the working dir + says so. args:
    pattern (required), path (optional), glob (optional file filter)."""
    import re as _re2
    import shutil as _sh
    pattern = args.get("pattern") or args.get("query") or ""
    if not pattern:
        return {"ok": False, "error": "missing 'pattern'"}
    base = str(_workspace_root() or cwd)
    want = args.get("path")
    note = ""
    target = base
    if want:
        cand = want if os.path.isabs(want) else os.path.join(base, want)
        if os.path.exists(cand):
            target = cand
        else:
            note = f"path {want!r} not found — searched the whole project instead"
    limit = int(args.get("limit", 80))
    glob = args.get("glob")
    rg = _sh.which("rg")
    if rg:
        cmd = [rg, "-n", "-i", "--no-heading", "-m", str(limit)]
        for d in _SKIP_DIRS:
            cmd += ["-g", f"!{d}"]
        if glob:
            cmd += ["-g", glob]
        cmd += [pattern, target]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            lines = (p.stdout or "").splitlines()[:limit]
            return {"ok": True, "matches": lines, "note": note,
                    "truncated": len(lines) >= limit}
        except Exception:  # noqa: BLE001
            pass  # fall through to python
    # Python fallback
    try:
        rx = _re2.compile(pattern, _re2.IGNORECASE)
    except _re2.error as e:
        return {"ok": False, "error": f"bad regex: {e}"}
    out: list[str] = []
    import fnmatch as _fn
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            # fnmatch handles *.py, test_*, *_spec.ts etc. (the old
            # endswith(glob.lstrip("*")) only matched suffix globs).
            if glob and not _fn.fnmatch(f, glob):
                continue
            fp = os.path.join(root, f)
            try:
                with open(fp, encoding="utf-8", errors="ignore") as fh:
                    for i, ln in enumerate(fh, 1):
                        if rx.search(ln):
                            out.append(f"{os.path.relpath(fp, base)}:{i}:{ln.rstrip()[:200]}")
                            if len(out) >= limit:
                                return {"ok": True, "matches": out, "note": note,
                                        "truncated": True}
            except Exception:  # noqa: BLE001
                continue
    return {"ok": True, "matches": out, "note": note, "truncated": False}


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
        # Derive a stable name from an explicit arg or the first line of text
        # (repo_rules keys the file by a slug of the name).
        name = (args.get("name") or "").strip()
        if not name:
            first = text.lstrip("-# ").splitlines()[0] if text else "rule"
            name = re.sub(r"\s+", " ", first).strip()[:60] or "rule"
        globs = args.get("globs")
        if isinstance(globs, str):
            globs = [g.strip() for g in globs.split(",") if g.strip()]
        # Unified artifact frontmatter (same shape as skills/workflows):
        # name / description / triggers / scope.
        description = (args.get("description") or "").strip()
        triggers = args.get("triggers")
        if isinstance(triggers, str):
            triggers = [t.strip() for t in triggers.split(",") if t.strip()]
        scope = (args.get("scope") or "global").lower()
        text = _elaborate_body("rule", text, name=name,
                               description=description)   # LLM format+elaborate
        res = repo_rules.write_rule(
            name, text, globs=globs, always=True,
            description=description, triggers=triggers, scope=scope)
        if not res.get("ok"):
            return res
        # Also record in knowledge memory so unified_query / recall surface the
        # rule alongside facts. scope=repo → tag to THIS repo; scope=global →
        # repo-agnostic (repo=None; recall unions NULL-repo rows so it applies
        # everywhere). Write directly to the embedded store — memory_write
        # refuses a null repo, which would silently drop global rules.
        scope = (args.get("scope") or "global").lower()
        try:
            from aiforge_core.memory import backend_select as _bsel
            if _bsel.embedded():
                from aiforge_core.memory import sqlite_memory as _sqlmem
                _sqlmem.write_unit(
                    text=f"RULE: {text}", kind="note", source="rule",
                    tags=["rule", scope],
                    repo=(_chat_repo_key(cwd) if scope == "repo" else None))
        except Exception:  # noqa: BLE001 — memory write must not block the rule
            pass
        return {"ok": True, "name": name, "scope": scope, "remembered": text,
                "path": res.get("path")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


_BULLET_TRIGGERS_RE = re.compile(r"^\[triggers:\s*([^\]]*)\]\s*(.*)$")


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


def _preferences_context(cwd: str) -> str:
    """The user's captured PREFERENCES (global defaults/conventions), injected
    every turn so a once-stated preference is always honoured and never
    re-asked. Stored as ``pref:``-tagged units by preference_capture; embedded
    backend only. Best-effort — never breaks the turn."""
    try:
        from aiforge_core.memory import backend_select as _bsel
        if not _bsel.embedded():
            return ""
        import json as _json
        from aiforge_core.memory import sqlite_memory as _m
        lines: list[str] = []
        with _m._conn() as c:  # noqa: SLF001 — internal read, best-effort
            for r in c.execute(
                "SELECT text, tags FROM memory_units WHERE kind='preference' "
                "ORDER BY id DESC LIMIT 40").fetchall():
                try:
                    tags = _json.loads(r["tags"] or "[]") or []
                except (TypeError, ValueError):
                    tags = []
                if any(isinstance(t, str) and t.startswith("pref:") for t in tags):
                    txt = (r["text"] or "").strip().replace("\n", " ")
                    if txt:
                        lines.append(f"- {txt[:240]}")
        if not lines:
            return ""
        return ("USER PREFERENCES (standing defaults/conventions the user set — "
                "apply them without asking again):\n" + "\n".join(lines[:40]))
    except Exception:  # noqa: BLE001
        return ""


def _rules_context(cwd: str, query: str = "") -> str:
    """The user's persistent rule book (global + this-repo), injected into
    EVERY session so the rules are always honoured. Untagged bullets are
    always-on (legacy). Bullets tagged with an inline '[triggers: ...]'
    prefix are gated by relevance to ``query`` via the shared scorer; a
    near-tie among tagged bullets injects an ASK note instead of silently
    picking one."""
    try:
        from aiforge_core.memory import md_store
        from aiforge_core.runtime import skills as _sk
        always_lines: list[str] = []
        tagged: list[_sk.Skill] = []
        # Align the repo rule key with the canonical recall key (_chat_repo_key,
        # git-toplevel) so a rule captured for this repo is read back under the
        # SAME key it was written — _repo_name (workspace/subdir basename) drifted.
        for src in ("rules:global", f"rules:{_chat_repo_key(cwd)}"):
            p = _cached_find_by_source(src)
            if p is None:
                continue
            body = md_store._parse(p).get("body", "")
            for line in body.splitlines():
                if not line.strip():
                    continue
                trig, text = _parse_bullet(line)
                if not trig:
                    always_lines.append("- " + text)
                else:
                    tagged.append(_sk.Skill(
                        name=text[:60], description="", triggers=trig,
                        body=text, source=src, always=False, priority=0))
        # ALSO read the repo_rules store — the SAME store the Library UI /
        # create-form / remember_rule write to. Use load_rules(cwd), which
        # merges builtin → global (~/.aiforge/rules) → repo-local and dedups BY
        # NAME with the most-specific winning: a REPO rule OVERRIDES a global
        # rule of the same name (parity with the team/pipeline path). An
        # always-on rule joins the always block; a glob-scoped rule becomes a
        # trigger-gated bullet so relevance scoring applies.
        try:
            from aiforge_core.runtime import repo_rules as _rr
            # load_rules needs a repo root; fall back to global-only when we're
            # not in a repo (still honours global rules).
            try:
                _rules = list(_rr.load_rules(cwd)) if cwd else list(
                    _rr.load_global_rules())
            except Exception:  # noqa: BLE001
                _rules = list(_rr.load_global_rules())
            for r in _rules:
                rt = (r.body or "").strip()
                if not rt:
                    continue
                if getattr(r, "always", True) or not getattr(r, "globs", None):
                    always_lines.append("- " + rt.replace("\n", " ")[:400])
                else:
                    tagged.append(_sk.Skill(
                        name=(r.name or rt[:60]), description="",
                        triggers=tuple(str(g).lower() for g in r.globs),
                        body=rt.replace("\n", " ")[:400], source="repo_rules",
                        always=False, priority=0))
        except Exception:  # noqa: BLE001 — repo_rules read is best-effort
            pass
        blocks: list[str] = list(always_lines)
        ambiguous_note = ""
        if tagged:
            # Scorer runs in its OWN guard: a select_or_ask defect must never
            # drop the legacy always-on `blocks` (the noise-fix turning into
            # a rules-vanish bug). On any scorer error, fail open — include
            # every tagged bullet, same as the no-query path.
            try:
                if query:
                    chosen, ambiguous = _sk.select_or_ask(
                        query, pool=tagged, k=len(tagged))
                    blocks.extend("- " + s.body for s in chosen)
                    if ambiguous:
                        names = " or ".join(f"'{s.body}'" for s in ambiguous[0])
                        ambiguous_note = (
                            "\nAMBIGUOUS RULE MATCH: " + names + " both matched"
                            " — ASK the user which applies before proceeding, "
                            "don't guess.")
                else:
                    # No query to score against (defensive) — fail open.
                    blocks.extend("- " + s.body for s in tagged)
            except Exception:  # noqa: BLE001 — fail open, keep always-on rules
                blocks.extend("- " + s.body for s in tagged)
        if not blocks:
            return ""
        # Rules are MANDATORY — never silently drop tail rules to a hard slice.
        # Cap is env-tunable and a truncation is called out so the agent knows
        # rules exist beyond the cut (and can look them up) instead of treating
        # the visible subset as complete.
        try:
            cap = max(400, int(os.environ.get("AIFORGE_RULES_MAX_CHARS", "4000")))
        except ValueError:
            cap = 4000
        body_txt = "\n".join(blocks)
        if len(body_txt) > cap:
            body_txt = (body_txt[:cap]
                        + "\n- …more rules truncated — call memory_lookup"
                          "(\"rules\") for the full list before acting")
        return ("RULES — MANDATORY, non-negotiable: the user ordered these "
                "ALWAYS followed, every session, HIGHEST priority (they "
                "override defaults and convenience). Check your plan against "
                "each rule before answering or acting:\n"
                + body_txt + ambiguous_note)
    except Exception:  # noqa: BLE001
        return ""
