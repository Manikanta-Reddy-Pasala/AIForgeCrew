from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from ._shell import (_resolve, _syntax_check, _workspace_root)

_GIT_TOPLEVEL_CACHE: dict[str, str | None] = {}


def _git_toplevel(cwd: str | None) -> str | None:
    """Repo root for ``cwd`` (``git rev-parse --show-toplevel``), cached and
    soft-failing to None outside a work tree. Lets a SUBDIR resolve the same
    repo key as the root (gap M3)."""
    if not cwd:
        return None
    key = str(cwd)
    if key in _GIT_TOPLEVEL_CACHE:
        return _GIT_TOPLEVEL_CACHE[key]
    top: str | None = None
    try:
        out = subprocess.run(
            ["git", "-C", key, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            top = out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — recall must never break on git
        top = None
    _GIT_TOPLEVEL_CACHE[key] = top
    return top


def _chat_repo_key(cwd: str | None) -> str:
    """Repo key for chat recall — resolves the GIT-TOPLEVEL basename (so a
    subdir recalls the same repo as the root), falling back to the raw cwd
    basename, then ``AIFORGE_AFM_REPO``, then the literal ``"repo"`` (gap M3).
    Note ``repo_key`` is always truthy for a real path, so its ``or env``
    fallback was dead — we chain the env explicitly here."""
    from aiforge_core.runtime import repo_ident as _ri
    return _ri.repo_name(cwd, sentinel="repo")


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


def _t_create_job_script(args: dict, cwd: str) -> dict:
    """JOB-BUILDER finalize: write the approved script to the local
    ~/.aiforge/jobs folder and register a cron job that RUNS it (deterministic
    — no ticket, no LLM per fire). Args: name, cron, script, optional
    description. Mirrors POST /api/jobs/script so the chat builder can finalize
    in-conversation."""
    try:
        name = str(args.get("name") or "").strip()
        cron = str(args.get("cron") or "").strip()
        script = str(args.get("script") or "")
        if not name or not cron or not script.strip():
            return {"ok": False, "error": "need name, cron, and script"}
        from aiforge_core.jobs import parse as jobs_parse
        from aiforge_core.jobs import scripts as jobs_scripts
        from aiforge_core.jobs import store as jobs_store
        if not jobs_parse.schedulable(cron):
            return {"ok": False,
                    "error": f"invalid or unschedulable cron: {cron!r}"}
        path = jobs_scripts.write_script(name, script)
        # TEST BEFORE SCHEDULE: run the script once. A wrong JQL/filter would
        # otherwise be scheduled as-is and fire forever doing nothing. On
        # failure, DON'T schedule and DON'T leave an orphan script. `skip_test`
        # (default off) is the escape for destructive/time-sensitive scripts.
        trial_output = None
        if not bool(args.get("skip_test")):
            trial = jobs_scripts.run_script(path)
            if not trial.get("ok"):
                jobs_scripts.delete_script(path)
                return {"ok": False, "tested": True,
                        "error": ("trial run FAILED (exit "
                                  f"{trial.get('returncode')}) — job NOT "
                                  "scheduled. Fix the script and retry.\n"
                                  f"STDOUT:\n{trial.get('stdout', '')}\n"
                                  f"STDERR:\n{trial.get('stderr', '')}")}
            trial_output = trial.get("stdout")
        # DEDUPE: replace any existing job(s) with the same name (+ their
        # script files) instead of piling up duplicates that all fire.
        replaced = []
        try:
            for j in jobs_store.list_jobs():
                if str(j.get("name") or "").strip().lower() == name.lower():
                    sp = j.get("script_path")
                    if sp and sp != path and jobs_scripts.is_within_jobs_dir(sp):
                        jobs_scripts.delete_script(sp)
                    jobs_store.delete(j["id"])
                    replaced.append(j["id"])
        except Exception:  # noqa: BLE001 — dedupe is best-effort, never block create
            pass
        nxt = jobs_parse.next_runs(cron, n=1)[0]
        job = jobs_store.create(
            name=name, cron=cron, ticket_title=name,
            ticket_body=(str(args.get("description") or "").strip()
                         or f"Runs script: {path}"),
            next_run_at=nxt, kind="script", script_path=path)
        return {"ok": True, "job_id": job["id"], "script_path": path,
                "human_schedule": jobs_parse.human_schedule(cron),
                "next_run_at": job["next_run_at"],
                "tested": not bool(args.get("skip_test")),
                "trial_output": trial_output,
                "replaced_jobs": replaced}
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


# Elaboration prompts — turn a user's rough input into a well-structured
# playbook BODY (no frontmatter; write_skill/write_workflow add that). Local
# models often emit a thin one-liner as the body; running it through the model
# once server-side guarantees a formatted, elaborated artifact.
_ELABORATE_PROMPT = {
    "skill": ("Rewrite the following into a clear, reusable SKILL body: a short "
              "intro line then concise numbered/bulleted steps the agent "
              "follows. Keep the user's intent; add the obvious missing detail. "
              "Output ONLY the markdown body — NO YAML frontmatter, no name."),
    "workflow": ("Rewrite the following into a WORKFLOW body: numbered "
                 "end-to-end steps, each concrete and in dependency order, with "
                 "a final done-check. Keep the user's intent; fill obvious gaps. "
                 "Output ONLY the markdown body — NO frontmatter."),
    "rule": ("Rewrite the following into a coding RULE: a '# Title' line then "
             "tight imperative bullet points the agent must follow. Keep the "
             "intent; make each bullet testable. Output ONLY the markdown."),
}


def _elaborate_body(kind: str, body: str, *, name: str = "",
                    description: str = "") -> str:
    """Format+elaborate a rough ``body`` via the model. Best-effort: returns the
    ORIGINAL body on any failure/empty, and skips when disabled or the body is
    already substantial (>= 400 chars with structure) so we don't over-rewrite a
    good doc. Off with AIFORGE_BUILDER_ELABORATE=0."""
    body = (body or "").strip()
    if os.environ.get("AIFORGE_BUILDER_ELABORATE", "1") in ("0", "false", "no"):
        return body
    prompt = _ELABORATE_PROMPT.get(kind)
    if not prompt or not body:
        return body
    # Already a structured, non-trivial doc → leave it (avoid churn).
    if len(body) >= 400 and ("\n" in body) and any(
            m in body for m in ("- ", "1.", "# ", "* ")):
        return body
    ctx = (f"Name: {name}\n" if name else "") + \
          (f"Purpose: {description}\n" if description else "")
    try:
        from aiforge_core.llm import client as _llm
        out = _llm.complete("architect", [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (ctx + "\nInput:\n" + body).strip()},
        ], max_tokens=900, temperature=0.2,
            timeout_s=int(os.environ.get("AIFORGE_BUILDER_ELABORATE_TIMEOUT_S", "45")))
        out = (out or "").strip()
        # Strip a stray ```markdown fence if the model wrapped it.
        if out.startswith("```"):
            parts = out.split("```")
            if len(parts) >= 2:
                out = parts[1]
                if out.lower().lstrip().startswith("markdown"):
                    out = out.lstrip()[8:]
                out = out.strip()
        return out or body
    except Exception:  # noqa: BLE001 — elaboration is best-effort, never block save
        return body


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


def _t_ensure_runtime(args: dict, cwd: str) -> dict:
    """Install + verify missing language runtimes / build tools so the
    agent can actually build & run the project."""
    try:
        from aiforge_core.runtime.tools.ensure_runtime import ensure_runtime
        tools = args.get("tools") or args.get("tool") or []
        return ensure_runtime(tools)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_project(args: dict, cwd: str) -> dict:
    """Detect/install/build/test/run any common stack (maven, gradle,
    node/react/next/vite, python, go, rust) with the canonical command."""
    try:
        from aiforge_core.runtime.tools.project_runner import project
        return project(action=args.get("action", "detect"),
                       cwd=args.get("cwd") or cwd,
                       timeout=int(args.get("timeout", 1800)))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_confluence_search(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_search(args, cwd)


def _t_confluence_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_read(args, cwd)


def _t_confluence_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_create(args, cwd)


def _t_confluence_update(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_update(args, cwd)


def _t_confluence_attach(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_attach(args, cwd)


def _t_set_repo_folder(args: dict, cwd: str) -> dict:
    """Persist the local FOLDER for a repo so tickets/pipeline runs for that
    repo resolve to it — ``repo`` = the project name, ``path`` = its absolute
    local folder. Use when the user says 'use /x/y for repo foo' or 'repo foo
    lives at /x/y'. Stored in repos.json; read by the workspace resolver."""
    from aiforge_core.config import repo_map
    repo = str(args.get("repo") or "").strip()
    path = str(args.get("path") or "").strip()
    if not repo or not path:
        return {"ok": False, "error": "need repo and path"}
    import os as _os
    if not _os.path.isdir(_os.path.expanduser(path)):
        return {"ok": False, "error": f"not a directory: {path}"}
    return repo_map.set_path(repo, path)


def _t_set_repo_root(args: dict, cwd: str) -> dict:
    """Persist the GLOBAL base folder that holds all repos — ``path`` = the
    directory whose subfolders are repos (a ticket for project ``foo`` resolves
    to ``<path>/foo``). Use when the user says 'all repos live under /x' or
    'the global repo folder is /x'."""
    from aiforge_core.config import repo_map
    path = str(args.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "need path"}
    import os as _os
    if not _os.path.isdir(_os.path.expanduser(path)):
        return {"ok": False, "error": f"not a directory: {path}"}
    return repo_map.set_default_root(path)


def _t_list_repos(args: dict, cwd: str) -> dict:
    """List the configured repo folders: the global base + explicit per-repo
    paths + the git repos found under the base."""
    from aiforge_core.config import repo_map
    import os as _os
    cfg = repo_map.list_all()
    root = cfg["default_root"]
    found = []
    try:
        for d in sorted(_os.listdir(root)):
            p = _os.path.join(root, d)
            if _os.path.isdir(_os.path.join(p, ".git")):
                found.append(d)
    except OSError:
        pass
    return {"ok": True, "default_root": root, "paths": cfg["paths"],
            "repos_under_root": found}


def _t_set_integration_default(args: dict, cwd: str) -> dict:
    """Persist a user-stated DEFAULT so later tool calls auto-fill it —
    ``tool`` = jira | confluence, ``value`` = the project key (jira) or space
    key (confluence). Deterministic: stored in the integrations config, read by
    jira_*/confluence_* on every call. Use when the user says e.g. 'use ENG as
    the default project' / 'default Confluence space is DEV'."""
    tool = str(args.get("tool") or "").strip().lower()
    value = str(args.get("value") or "").strip()
    if tool not in ("jira", "confluence"):
        return {"ok": False, "error": "tool must be 'jira' or 'confluence'"}
    if not value:
        return {"ok": False, "error": "missing 'value' (project/space key)"}
    field = "default_project" if tool == "jira" else "default_space"
    try:
        from aiforge_core.config import integrations
        integrations.set_(tool, {field: value})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "tool": tool, field: value,
            "note": f"{tool} calls will now default {field}={value} when omitted"}


def _t_jira_search(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_search(args, cwd)


def _t_jira_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_read(args, cwd)


def _t_jira_worklog(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_worklog(args, cwd)


def _t_jira_remote_links(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_remote_links(args, cwd)


def _t_resolve_repo(args: dict, cwd: str) -> dict:
    """Resolve a loosely-typed repo/service/folder name to its local path
    (tolerates case, spaces, missing hyphens, typos)."""
    from aiforge_core.config import repo_map
    return repo_map.resolve(args.get("name") or args.get("repo") or "")


def _t_jira_resolve_project(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_resolve_project(args, cwd)


def _t_confluence_resolve_space(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_resolve_space(args, cwd)


def _t_context_gather(args: dict, cwd: str) -> dict:
    """Assemble a cross-entity dossier (a Jira ticket + its linked Confluence
    pages + images, or vice versa) in PARALLEL, cache it in the context folder,
    and refresh only when the entity changed. Use when asked to explain/
    understand a ticket or page."""
    from aiforge_core.runtime import context_gather as _cg
    kind = (args.get("kind") or "").lower()
    key = str(args.get("key") or args.get("id") or "").strip()
    if not kind and key:
        # infer: a JIRA-KEY looks like PROJ-42 (case-insensitive); else a
        # numeric id → confluence. Normalize a jira key to uppercase.
        if re.match(r"^[A-Za-z][A-Za-z0-9]+-\d+$", key):
            kind, key = "jira", key.upper()
        else:
            kind = "confluence"
    if kind not in ("jira", "confluence") or not key:
        return {"ok": False, "error": "need kind (jira|confluence) + key/id"}
    return _cg.gather(kind, key, force=bool(args.get("force")),
                      role="chat")


def _t_note_curate(args: dict, cwd: str) -> dict:
    """Re-verify a managed workspace note (ticket.md/page.md/dossier note):
    re-fetch the source, refresh drifted Facts, flag dead links, and log each
    change under ## Learnings. Path defaults to the bound context's note.
    NOT in _READONLY_TOOLS — it WRITES the note; it stays ungated (ALLOW)
    because the curator's own path jail confines writes to the managed
    work root (see note_curator)."""
    from aiforge_core.runtime import note_curator
    path = str(args.get("path") or "").strip()
    if not path:
        path = note_curator.primary_note_for_cwd(cwd) or ""
    if not path:
        return {"ok": False,
                "error": "no managed note found — pass 'path' or run inside "
                         "a jira/confluence context workspace"}
    return note_curator.curate_note(path, cwd=cwd)


def _t_note_consolidate(args: dict, cwd: str) -> dict:
    """Intelligently fold NEW knowledge into a managed note's OKR sections:
    an LLM dedupes paraphrases, resolves contradictions, and MAPS each item to
    the right section (Objective/Key Results/Facts/Links/Learnings); large input
    is chunked on structure boundaries. Path defaults to the bound context's
    note. WRITES — jailed to the managed work root (same boundary as
    note_curate), so it stays ungated."""
    from aiforge_core.runtime import note_curator, work_notes
    text = str(args.get("text") or args.get("content") or "").strip()
    if not text:
        return {"ok": False, "error": "pass 'text' — the new knowledge to fold "
                                      "into the note"}
    path = str(args.get("path") or "").strip()
    if not path:
        path = note_curator.primary_note_for_cwd(cwd) or ""
    if not path:
        return {"ok": False,
                "error": "no managed note found — pass 'path' or run inside "
                         "a jira/confluence context workspace"}
    if not note_curator._inside_work_root(path):
        return {"ok": False,
                "error": "path outside the managed work root — refusing"}
    return work_notes.consolidate_note(path, text, role="learner")


def _t_codegraph_query(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import codegraph
    return codegraph.codegraph_query(args, cwd)


def _t_codegraph_callers(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import codegraph
    return codegraph.codegraph_callers(args, cwd)


def _t_codegraph_callees(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import codegraph
    return codegraph.codegraph_callees(args, cwd)


def _t_codegraph_impact(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import codegraph
    return codegraph.codegraph_impact(args, cwd)


def _t_codegraph_explore(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import codegraph
    return codegraph.codegraph_explore(args, cwd)


def _t_jira_log_work(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_log_work(args, cwd)


def _t_jira_myself(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_myself(args, cwd)


def _t_jira_projects(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_projects(args, cwd)


def _t_jira_boards(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_boards(args, cwd)


def _t_jira_sprints(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_sprints(args, cwd)


def _t_jira_sprint_issues(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_sprint_issues(args, cwd)


def _t_jira_dashboards(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_dashboards(args, cwd)


def _t_jira_dashboard_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_dashboard_read(args, cwd)


def _t_jira_dashboard_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_dashboard_create(args, cwd)


def _t_jira_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_create(args, cwd)


def _t_jira_update(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_update(args, cwd)


def _t_jira_comment(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_comment(args, cwd)


def _t_email_send(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import email_tool
    return email_tool.email_send(args, cwd)


def _t_email_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import email_tool
    return email_tool.email_read(args, cwd)


def _t_gitlab_search(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_search(args, cwd)


def _t_gitlab_read(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_read(args, cwd)


def _t_gitlab_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_create(args, cwd)


def _t_gitlab_update(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_update(args, cwd)


def _t_gitlab_comment(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_comment(args, cwd)


def _t_gitlab_mr_create(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_mr_create(args, cwd)


def _t_gitlab_mr_comment(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import gitlab
    return gitlab.gitlab_mr_comment(args, cwd)


def _t_github_pr(args: dict, cwd: str) -> dict:
    """Open a GitHub pull request from the current branch via the ``gh`` CLI.
    Args: title (req), body, base (default 'main'), head (default current
    branch), draft. Requires gh installed + authenticated in the repo."""
    if not args.get("title"):
        return {"ok": False, "error": "missing 'title'"}
    import shutil
    if not shutil.which("gh"):
        return {"ok": False, "error": "gh_not_installed",
                "hint": "install the GitHub CLI (gh) + `gh auth login`"}
    cmd = ["gh", "pr", "create", "--title", str(args["title"]),
           "--body", str(args.get("body") or "")]
    cmd += ["--base", str(args.get("base") or "main")]
    if args.get("head"):
        cmd += ["--head", str(args["head"])]
    if args.get("draft"):
        cmd += ["--draft"]
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr or out or "gh failed").strip()[:800]}
    return {"ok": True, "url": out, "written": {"title": args.get("title"),
            "base": args.get("base") or "main"}}


def _t_web_search(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import web_search
    return web_search.web_search(args, cwd)


def _t_web_fetch(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import web_search
    return web_search.web_fetch(args, cwd)


def _t_web_crawl(args: dict, cwd: str) -> dict:
    """Fetch a URL as clean markdown and file it as a work/web/<slug> dossier
    (crawl4ai when installed, tag-strip fetch fallback)."""
    from aiforge_core.runtime.tools import web_ingest
    return web_ingest.web_crawl(args, cwd)


def _t_serve(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import serve
    return serve.serve(args, cwd)


def _t_stop_service(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import serve
    return serve.stop_service(args, cwd)


def _t_list_services(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import serve
    return serve.list_services(args, cwd)


def _t_skill_search(args: dict, cwd: str) -> dict:
    """Search the skill registry (SKILL.md playbooks) by relevance."""
    try:
        from aiforge_core.runtime import skills as _skills
        q = args.get("query") or args.get("q") or ""
        hits = _skills.search(q, cwd, k=int(args.get("k", 5)))
        return {"ok": True, "skills": hits}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_learn_skill(args: dict, cwd: str) -> dict:
    """Author a reusable skill (SKILL.md) so future sessions reuse the
    solution. scope: 'global' (all repos) or 'repo' (this repo)."""
    try:
        from aiforge_core.runtime import skills as _skills
        triggers = args.get("triggers") or []
        if isinstance(triggers, str):
            triggers = [t.strip() for t in triggers.split(",") if t.strip()]
        _name = args.get("name", "")
        _desc = args.get("description", "")
        _body = _elaborate_body("skill", args.get("body") or args.get("content")
                                or "", name=_name, description=_desc)
        return _skills.write_skill(
            name=_name, description=_desc, body=_body,
            triggers=list(triggers), cwd=cwd,
            scope=(args.get("scope") or "global").lower(),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_workflow_search(args: dict, cwd: str) -> dict:
    """Search the workflow registry (WORKFLOW.md procedures) by relevance."""
    try:
        from aiforge_core.runtime import workflows as _wf
        q = args.get("query") or args.get("q") or ""
        return {"ok": True, "workflows": _wf.search(q, cwd, k=int(args.get("k", 5)))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_learn_workflow(args: dict, cwd: str) -> dict:
    """Author a reusable workflow (WORKFLOW.md) — an end-to-end procedure —
    so future sessions (or the user) can reuse it. scope: 'global' or 'repo'.
    Optional ``scripts`` land in the workflow's own ``scripts/`` folder;
    write_workflow HARD-tests each one (syntax check + actually RUNS its
    ``test`` command or the script itself) and REFUSES the save on any
    failure — job-builder parity, no honour-system flag."""
    try:
        from aiforge_core.runtime import workflows as _wf
        triggers = args.get("triggers") or []
        if isinstance(triggers, str):
            triggers = [t.strip() for t in triggers.split(",") if t.strip()]
        scripts = args.get("scripts") or []
        _name = args.get("name", "")
        _desc = args.get("description", "")
        _body = _elaborate_body("workflow", args.get("body") or args.get("content")
                                or "", name=_name, description=_desc)
        return _wf.write_workflow(
            name=_name, description=_desc, body=_body,
            triggers=list(triggers), cwd=cwd,
            scope=(args.get("scope") or "global").lower(),
            scripts=scripts,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ─────────────────────────── shared "strong" tools ──────────────────────────
# The OpenHands-parity tools (editor with undo + syntax-check, LSP, typecheck,
# format, test-runner) lived only in the ADK team pipeline. These thin adapters
# expose them to the deploy-anywhere chat agent too. They resolve through
# sandbox.root(); the dispatch loop scopes that override to the WORKSPACE root
# (set+reset in finally) for exactly these names — so a path can't escape an
# AIFORGE_WORKSPACE_DIR jail and a reused thread can't leak the dir.
# ipython (execute_ipython_cell) IS exposed to chat for Claude-Code/Cursor
# parity, but — because it runs arbitrary code in a kernel — it is
# approval-gated (in tool_policy._DEFAULT_ASK → ASK in Act mode, blocked in
# Plan mode) AND cwd-jailed here, so it can't run unapproved or escape the
# AIFORGE_WORKSPACE_DIR root the way the old unmanaged version did.
_ROOT_SCOPED_TOOLS = {"editor", "typecheck", "format", "lsp", "run_tests",
                      "execute_ipython_cell"}


def _scoped_root(cwd: str) -> str:
    """Root the strong tools should resolve against. Use the session ``cwd`` (so
    they hit the SAME files as file_read/file_write/multi_edit, and each parallel
    worktree stays isolated). Only when an AIFORGE_WORKSPACE_DIR jail is set AND
    cwd escapes it do we clamp to the jail root — so the strong tools can't write
    outside the jail, without collapsing every subtask onto one shared dir."""
    try:
        ws = _workspace_root()
        if ws is None:
            return cwd
        c = Path(cwd).expanduser().resolve()
        return str(c) if (c == ws or ws in c.parents) else str(ws)
    except Exception:  # noqa: BLE001
        return cwd


def _coerce_int(v, default=None):
    try:
        return int(v) if v is not None and str(v).strip() != "" else default
    except (TypeError, ValueError):
        return default


def _t_editor(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools.editor import editor
    vr = args.get("view_range")
    if isinstance(vr, list):
        vr = [_coerce_int(x) for x in vr]
        if any(x is None for x in vr):
            vr = None
    return editor(
        command=str(args.get("command") or args.get("sub_command") or "view"),
        path=str(args.get("path") or ""),
        file_text=args.get("file_text") if args.get("file_text") is not None else args.get("content"),
        old_str=args.get("old_str") if args.get("old_str") is not None else args.get("old_text"),
        new_str=args.get("new_str") if args.get("new_str") is not None else args.get("new_text"),
        insert_line=_coerce_int(args.get("insert_line")),
        view_range=vr,
    )


def _t_multi_edit(args: dict, cwd: str) -> dict:
    """Apply a BATCH of find/replace edits across one or more files in a single
    call — validated first, then applied atomically (snapshot + rollback). Each
    edit: ``{"path","old_str","new_str","replace_all"?}``."""
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits:
        return {"ok": False, "error": "edits must be a non-empty list of "
                "{path, old_str, new_str, replace_all?}"}
    pending: dict[str, str] = {}        # abs_path -> working content (chained)
    original: dict[str, str] = {}       # abs_path -> pre-edit disk content (rollback)
    rel_of: dict[str, str] = {}         # abs_path -> the path the model gave
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            return {"ok": False, "error": f"edit #{i} is not an object"}
        path = str(e.get("path") or "").strip()
        old = e.get("old_str") if e.get("old_str") is not None else e.get("old_text")
        new = e.get("new_str") if e.get("new_str") is not None else e.get("new_text")
        if not path or old is None or new is None:
            return {"ok": False, "error": f"edit #{i} needs path + old_str + new_str"}
        if old == "":
            return {"ok": False, "error": f"edit #{i}: old_str must be non-empty"}
        try:
            ap = str(_resolve(cwd, path))
        except PermissionError as exc:
            return {"ok": False, "error": str(exc)}
        rel_of.setdefault(ap, path)
        if ap not in pending:
            try:
                pending[ap] = Path(ap).read_text(encoding="utf-8", errors="replace")
                original[ap] = pending[ap]
            except FileNotFoundError:
                return {"ok": False, "error": f"edit #{i}: file not found: {path}"}
        body = pending[ap]
        cnt = body.count(old)
        if cnt == 0:
            return {"ok": False, "error": f"edit #{i}: old_str not found in {path}"}
        if cnt > 1 and not e.get("replace_all"):
            return {"ok": False, "error": f"edit #{i}: old_str appears {cnt}× in "
                    f"{path} — pass replace_all:true or make it unique"}
        pending[ap] = body.replace(old, new) if e.get("replace_all") else body.replace(old, new, 1)
    # Syntax-guard each resulting code file (skipped for non-code / force:true).
    for ap, content in pending.items():
        bad = _syntax_check(ap, content, args)
        if bad:
            return {"ok": False, "error": "syntax_invalid", "file": rel_of.get(ap, ap),
                    "detail": bad, "hint": "fix the edit or pass force:true"}
    # Phase 2 — write atomically: on ANY failure, roll every file back.
    written: list[str] = []
    done: list[str] = []
    try:
        for ap, content in pending.items():
            Path(ap).write_text(content, encoding="utf-8")
            done.append(ap)
            written.append(rel_of.get(ap, ap))
    except Exception as exc:  # noqa: BLE001 — restore the pre-edit state
        for ap in done:
            try:
                Path(ap).write_text(original[ap], encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        return {"ok": False, "error": f"write failed, rolled back: {exc}"}
    return {"ok": True, "files": written, "edits_applied": len(edits)}


def _t_typecheck(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools.typecheck import typecheck
    return typecheck()


def _t_format(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools.format import format as _fmt
    return _fmt(str(args.get("path") or "."))


def _t_lsp(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools.lsp import lsp
    return lsp(command=str(args.get("command") or ""), path=str(args.get("path") or ""),
               line=_coerce_int(args.get("line"), 0), character=_coerce_int(args.get("character"), 0))


def _t_run_tests(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools.test_runner import run_tests
    return run_tests(mode=str(args.get("mode") or "fast"), pattern=str(args.get("pattern") or ""))


def _git_cli(argv: list, cwd: str, timeout: int = 30) -> dict:
    import subprocess
    try:
        r = subprocess.run(["git", *argv], cwd=cwd or ".", capture_output=True,
                           text=True, timeout=timeout)
        return {"ok": r.returncode == 0, "code": r.returncode,
                "stdout": (r.stdout or "")[-8000:], "stderr": (r.stderr or "")[-2000:]}
    except FileNotFoundError:
        return {"ok": False, "error": "git_not_installed"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _t_git_status(args: dict, cwd: str) -> dict:
    return _git_cli(["status", "--porcelain=v1", "-b"], cwd)


def _t_git_diff(args: dict, cwd: str) -> dict:
    argv = ["--no-pager", "diff"] + (["--staged"] if args.get("staged") else [])
    if args.get("path"):
        argv += ["--", str(args["path"])]
    return _git_cli(argv, cwd)


def _t_git_log(args: dict, cwd: str) -> dict:
    n = max(1, min(_coerce_int(args.get("limit"), 20) or 20, 200))
    argv = ["--no-pager", "log", f"-{n}", "--oneline", "--decorate"]
    if args.get("path"):
        argv += ["--", str(args["path"])]
    return _git_cli(argv, cwd)


def _t_jira_transitions(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_transitions(args, cwd)


def _t_jira_transition(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_transition(args, cwd)


def _t_jira_assign(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_assign(args, cwd)


def _t_jira_link_issues(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import jira
    return jira.jira_link_issues(args, cwd)


def _t_confluence_children(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_children(args, cwd)


def _t_confluence_attach(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_attach(args, cwd)


def _t_confluence_spaces(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_spaces(args, cwd)


def _t_confluence_page_by_title(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_page_by_title(args, cwd)


def _t_confluence_labels(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_labels(args, cwd)


def _t_confluence_add_label(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_add_label(args, cwd)


def _t_confluence_comments(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_comments(args, cwd)


def _t_confluence_comment(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_comment(args, cwd)


def _t_confluence_descendants(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import confluence
    return confluence.confluence_descendants(args, cwd)


def _t_git_blame(args: dict, cwd: str) -> dict:
    argv = ["--no-pager", "blame", "--date=short"]
    _s, _e = _coerce_int(args.get("start")), _coerce_int(args.get("end"))
    if _s and _e:
        argv += ["-L", f"{_s},{_e}"]
    argv += ["--", str(args.get("path") or "")]
    return _git_cli(argv, cwd)


def _t_read_lines(args: dict, cwd: str) -> dict:
    import os as _os
    path = str(args.get("path") or "")
    fp = path if _os.path.isabs(path) else _os.path.join(cwd or ".", path)
    try:
        with open(fp, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return {"ok": False, "error": f"not found: {path}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    n = len(lines)
    s = max(1, _coerce_int(args.get("start"), 1) or 1)
    e = n if not args.get("end") else min(_coerce_int(args.get("end"), n), n)
    if s > n:
        return {"ok": True, "path": path, "total_lines": n, "text": ""}
    return {"ok": True, "path": path, "start": s, "end": e, "total_lines": n,
            "text": "".join(lines[s - 1:e][:5000])[:60000]}


def _t_rename_symbol(args: dict, cwd: str) -> dict:
    import os as _os
    import re as _re
    name = str(args.get("name") or "")
    new = str(args.get("new_name") or "")
    if not name or not new:
        return {"ok": False, "error": "need 'name' and 'new_name'"}
    dry = args.get("dry_run", True)
    base = str(args.get("path") or ".")
    root_p = base if _os.path.isabs(base) else _os.path.join(cwd or ".", base)
    pat = _re.compile(r"\b" + _re.escape(name) + r"\b")
    _EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs", ".c",
            ".cpp", ".h", ".cs", ".rb", ".php", ".kt", ".scala", ".swift")
    hits, changed = [], 0
    for dp, dn, fns in _os.walk(root_p):
        dn[:] = [d for d in dn if d not in (".git", "node_modules", ".venv",
                 "venv", "dist", "build", "__pycache__")]
        for fn in fns:
            if not fn.endswith(_EXT):
                continue
            fpath = _os.path.join(dp, fn)
            try:
                with open(fpath, encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
            except Exception:  # noqa: BLE001
                continue
            c = len(pat.findall(txt))
            if not c:
                continue
            hits.append({"file": _os.path.relpath(fpath, cwd or "."),
                         "occurrences": c})
            if not dry:
                try:
                    with open(fpath, "w", encoding="utf-8") as fh:
                        fh.write(pat.sub(new, txt))
                    changed += c
                except Exception:  # noqa: BLE001
                    pass
    return {"ok": True, "name": name, "new_name": new, "dry_run": bool(dry),
            "files": hits, "total_occurrences": sum(h["occurrences"] for h in hits),
            "applied": (0 if dry else changed)}


def _chat_run_id(cwd: str) -> str:
    """Stable per-workspace id so the browser tab / IPython kernel PERSIST
    across chat turns.

    Tool handlers only receive ``(args, cwd)`` — the chat ``session_id`` is not
    threaded down to them — so we derive a deterministic id from ``cwd``. Using
    a content hash (not the salted builtin ``hash``) keeps it stable across
    process restarts, so a reconnecting session reattaches to the same tab.
    """
    import hashlib
    digest = hashlib.md5((cwd or ".").encode("utf-8")).hexdigest()[:12]
    return f"chat-{digest}"


# --- Pipeline-parity tools: mcp, browser, jupyter, sub-agent delegate -------
# The team pipeline Doer has these four; the SIMPLE-CHAT agent now matches it
# (and Claude Code / Cursor, which expose browser + MCP + sub-agents in a single
# agent). All degrade soft: if the dep (playwright / jupyter_client) or import
# is unavailable, the handler returns {"ok": False, "error": ...} instead of
# raising into the chat loop.
def _t_mcp(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.runtime.tools.mcp_client import mcp
        return mcp(str(args.get("command") or ""),
                   endpoint=args.get("endpoint"),
                   tool=args.get("tool"),
                   arguments=args.get("arguments"))
    except Exception as exc:  # noqa: BLE001 — soft-fail, never crash the chat
        return {"ok": False, "error": str(exc)}


def _t_browse(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.runtime.tools.browser import browse
        return browse(str(args.get("command") or ""),
                      url=args.get("url"),
                      path=args.get("path"),
                      selector=args.get("selector"),
                      text=args.get("text"),
                      x=_coerce_int(args.get("x")),
                      y=_coerce_int(args.get("y")),
                      button=args.get("button"),
                      key=args.get("key"),
                      dx=_coerce_int(args.get("dx")),
                      dy=_coerce_int(args.get("dy")),
                      _run_id=_chat_run_id(cwd))
    except Exception as exc:  # noqa: BLE001 — playwright may be absent
        return {"ok": False, "error": str(exc)}


def _t_ipython(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.runtime.tools.ipython_kernel import execute_ipython_cell
        kwargs: dict = {"_run_id": _chat_run_id(cwd)}
        _timeout = _coerce_int(args.get("timeout"))
        if _timeout is not None:
            kwargs["timeout"] = _timeout
        return execute_ipython_cell(str(args.get("code") or ""), **kwargs)
    except Exception as exc:  # noqa: BLE001 — jupyter_client may be absent
        return {"ok": False, "error": str(exc)}


def _t_delegate(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.runtime.tools.delegation import delegate_to_agent
        kwargs: dict = {}
        _timeout = _coerce_int(args.get("timeout"))
        if _timeout is not None:
            kwargs["timeout"] = _timeout
        return delegate_to_agent(str(args.get("role") or ""),
                                 str(args.get("prompt") or ""), **kwargs)
    except Exception as exc:  # noqa: BLE001 — soft-fail
        return {"ok": False, "error": str(exc)}


