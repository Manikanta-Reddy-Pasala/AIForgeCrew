"""Analysis fan-out — read N repos / explore M topics IN PARALLEL, then
synthesize one draft deliverable (report / Confluence-page markdown).

Why a separate pipeline: the CODE pipeline (parallel_subtasks) decomposes a
BUILD by file and each subtask writes + tests code. An ANALYSIS task ("read
these 3 repos, explore auth + sync + data-model, write it up") must instead:

  1. decompose BY REPO (topics explored WITHIN each repo),
  2. run one READ-ONLY explore agent per repo in parallel — in the repo's REAL
     directory (no worktree; write tools are forbidden so the repo is never
     mutated),
  3. SYNTHESIZE all per-repo findings into a single draft document.

Draft-only: the synthesis is returned as markdown; nothing is published to
Confluence/Jira (the router already strips publish intent unless the user says
publish/post).

Public API:
  identify_repos(prompt, cwd)  -> [{name, path}]
  extract_topics(prompt)       -> [str]
  should_fan_out(prompt, cwd)  -> (bool, repos, topics)
  stream_analysis_team(...)    -> generator of chat SSE events
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import re

_log = logging.getLogger("aiforge.analysis")

# Cap parallel explore agents — reuse the code pipeline's knob so an operator
# tunes one number. A serial local endpoint gains nothing above 1.
def _max_workers() -> int:
    try:
        n = int(os.environ.get("AIFORGE_PARALLEL_SUBTASKS_MAX", "4"))
    except (TypeError, ValueError):
        n = 4
    return max(1, min(n, 8))


def _is_git_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


def identify_repos(prompt: str, cwd: str) -> list[dict]:
    """Resolve which repositories this analysis spans, as ``[{name, path}]``.

    Sources, in order (deduped by resolved absolute path):
      1. Registry names (repos.json) whose name appears as a word in the prompt.
      2. Absolute/~ filesystem paths in the prompt that are real directories.
      3. Immediate child dirs of ``cwd`` that are themselves git repos (the
         user pinned a PARENT folder holding several repos).
      4. Fallback: ``cwd`` itself (single repo).
    """
    found: dict[str, dict] = {}   # abspath -> {name, path}

    def _add(name: str, path: str) -> None:
        if not path:
            return
        ap = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(ap) and ap not in found:
            found[ap] = {"name": name or os.path.basename(ap.rstrip("/")), "path": ap}

    p = prompt or ""
    plow = p.lower()
    try:
        cap = max(2, int(os.environ.get("AIFORGE_ANALYSIS_MAX_REPOS", "12")))
    except (TypeError, ValueError):
        cap = 12
    # Common words that are ALSO real repo names (web/api/pos/core/bot/...) —
    # matching them in prose spuriously pulls a repo in. Require a specific name.
    _common = {"web", "api", "app", "pos", "core", "bot", "cli", "crud", "ui",
               "db", "lib", "docs", "server", "client", "code", "main", "test"}

    # 1. registry names mentioned in the prompt. A SPECIFIC name (len>=4, not a
    #    common word) is a real signal on a plain mention. A common/short name
    #    (core/web/pos/erp) matches too much in prose, so require a repo CUE —
    #    backticks around it, or the word "repo"/"repository" adjacent — so an
    #    EXPLICITLY named short repo ("the `core` repo") is still recovered.
    try:
        from aiforge_core.config import repo_map as _rm
        paths = (_rm.list_all() or {}).get("paths") or {}
        for name, path in paths.items():
            nlow = str(name).strip().lower()
            if not nlow:
                continue
            esc = re.escape(nlow)
            word = re.search(r"\b" + esc + r"\b", plow)
            specific = len(nlow) >= 4 and nlow not in _common
            cued = (re.search(r"`\s*" + esc + r"\s*`", plow)
                    or re.search(r"\b" + esc + r"\b[\s\w]{0,12}\brepo",
                                 plow)
                    or re.search(r"\brepo(?:sitor(?:y|ies))?\b[\s\w]{0,12}\b"
                                 + esc + r"\b", plow))
            if (specific and word) or cued:
                _add(str(name), str(path))
    except Exception:  # noqa: BLE001 — registry optional
        pass

    # 2. explicit filesystem paths in the prompt — ONLY if they are git repos
    #    (an analysis targets repos, not an incidental /etc/nginx mention).
    for m in re.findall(r"(?:~|/)[\w./\-]+", p):
        ap = os.path.abspath(os.path.expanduser(m.rstrip("/")))
        if _is_git_repo(ap):
            _add(os.path.basename(ap), ap)

    # 3. child git repos of a pinned PARENT — ONLY when the prompt named nothing
    #    specific (sources 1+2 empty). Otherwise "summarize repoA" in a parent
    #    holding 10 checkouts would fan out over all 10.
    if not found:
        try:
            if os.path.isdir(cwd):
                for entry in sorted(os.listdir(cwd)):
                    child = os.path.join(cwd, entry)
                    if _is_git_repo(child):
                        _add(entry, child)
        except Exception:  # noqa: BLE001
            pass

    if found:
        out = list(found.values())
        if len(out) > cap:
            _log.warning("identify_repos: capped %d repos to %d (set "
                         "AIFORGE_ANALYSIS_MAX_REPOS)", len(out), cap)
            out = out[:cap]
        # Disambiguate duplicate basenames (two repos both named `api`) — the
        # name is used as the subtask slug, and a collision makes the panel flip
        # both rows together. Suffix collisions with the parent dir.
        _seen: dict[str, int] = {}
        for r in out:
            _seen[r["name"]] = _seen.get(r["name"], 0) + 1
        for r in out:
            if _seen[r["name"]] > 1:
                _parent = os.path.basename(os.path.dirname(r["path"])) or "?"
                r["name"] = f"{r['name']} ({_parent})"
        return out
    # 4. fallback — the pinned folder itself
    return [{"name": os.path.basename(os.path.abspath(cwd).rstrip("/")) or "repo",
             "path": os.path.abspath(cwd)}]


def extract_topics(prompt: str) -> list[str]:
    """Best-effort topic list from the prompt (heuristic, no LLM).

    Looks for an explicit enumeration after a cue word ('topics', 'explore',
    'analyze', 'on', 'about', 'cover', 'including') and splits it on commas /
    'and'. Returns [] when none is found — the explore agent then does a
    general overview."""
    # Strip filesystem paths first — otherwise "analyze /home/ai/codeRepo/X and
    # /home/ai/codeRepo/Y" turns the path segments ("home", "codeRepo", the repo
    # names) into bogus "topics".
    p = re.sub(r"(?:~|/)[\w./\-]+", " ", prompt or "")
    m = re.search(
        r"\b(?:topics?|explore|analy[sz]e|cover(?:ing)?|about|on|including|"
        r"focus(?:ing)?\s+on)\b[:\s]+(.{3,240})", p, re.IGNORECASE)
    if not m:
        return []
    tail = m.group(1)
    # stop at a sentence boundary / deliverable clause
    tail = re.split(r"(?:\band\s+(?:create|write|make|produce|generate)\b|"
                    r"[.;\n]|\bthen\b)", tail, maxsplit=1)[0]
    parts = re.split(r"\s*(?:,|/|\band\b|\bor\b)\s*", tail)
    topics = [t.strip(" .-") for t in parts if len(t.strip(" .-")) > 2]
    # drop obvious non-topics (verbs/filler that leaked in)
    stop = {"the", "these", "them", "each", "all", "repos", "repo",
            "repositories", "code", "codebase", "them independently"}
    topics = [t for t in topics if t.lower() not in stop]
    return topics[:8]


def should_fan_out(prompt: str, cwd: str) -> tuple[bool, list[dict], list[str]]:
    """Fan out iff the analysis spans ≥2 repos. A single repo (even with many
    topics) is handled by one research agent — no cross-repo parallelism to
    gain. Returns ``(fan_out, repos, topics)``."""
    repos = identify_repos(prompt, cwd)
    topics = extract_topics(prompt)
    return (len(repos) >= 2, repos, topics)


def _explore_one(repo: dict, topics: list[str], overall: str) -> dict:
    """One READ-ONLY explore agent on ``repo``. Runs the researcher role (no
    file_write/patch/bash — repo is never mutated) in the repo's REAL dir and
    returns its findings markdown. Autonomous (session_id=None) so no per-tool
    approval gates fire on a pure read."""
    try:
        from aiforge_core.llm.client import complete as _complete
        from aiforge_core.runtime.chat_agent import run_chat_agent
    except Exception as exc:  # noqa: BLE001
        return {"name": repo["name"], "path": repo["path"], "ok": False,
                "error": f"import: {exc}", "findings": ""}
    topic_line = ("Focus topics: " + "; ".join(topics) + "\n" if topics
                  else "Give a structured overview of the repository.\n")
    msg = (
        f"READ-ONLY analysis of the repository `{repo['name']}` at "
        f"`{repo['path']}`. Do NOT modify, create, or delete any file — this is "
        f"an inspection only.\n\n{topic_line}\n"
        f"Overall request (for context): {overall.strip()[:800]}\n\n"
        "Use repo_map / grep / file_read / codegraph to inspect the code. "
        "Produce a concise structured markdown findings report: for each topic, "
        "the key files/symbols (as path:line where possible), how it works, and "
        "any notable risks or gaps. End with a 3-5 bullet summary. Return ONLY "
        "the findings markdown as your final answer.")

    def complete_fn(role, convo):
        return _complete(role, convo)

    # Bind the repo root to THIS worker thread's context. ThreadPoolExecutor
    # does NOT copy contextvars, and codegraph._repo() resolves via
    # get_repo_root() BEFORE its cwd arg — so without this, a concurrent team
    # run's process-global AIFORGE_REPO_ROOT env would make every explore
    # resolve codegraph to the WRONG repo. Setting the contextvar here makes it
    # win over the shared env, and reset restores the worker's context.
    _root_tok = None
    try:
        from aiforge_core.runtime import request_context as _rc
        _root_tok = _rc.set_repo_root(repo["path"])
    except Exception:  # noqa: BLE001
        _rc = None

    findings, ok = "", False
    try:
        # mode="analyze" is the HARD read-only guard AND asks for FINDINGS (not
        # a change-plan): run_chat_agent's tool gate blocks any tool not in
        # _READONLY_TOOLS (file_write/patch/bash/confluence_create/... blocked;
        # file_read/grep/repo_map/codegraph allowed). role= does NOT restrict
        # tools in the chat loop and session_id=None skips the approval gate, so
        # WITHOUT read-only mode a hallucinated write would auto-apply in the
        # user's REAL repo (no worktree here) — read-only is mandatory.
        for ev in run_chat_agent([{"role": "user", "content": msg}],
                                 cwd=repo["path"], role="researcher",
                                 session_id=None, mode="analyze",
                                 complete_fn=complete_fn):
            if ev.get("type") == "error":
                return {"name": repo["name"], "path": repo["path"], "ok": False,
                        "error": ev.get("text"), "findings": ""}
            if ev.get("type") == "message" and not ev.get("awaiting_input"):
                txt = ev.get("text") or ""
                if txt and not txt.startswith("(stopped:"):
                    findings, ok = txt, True
    except Exception as exc:  # noqa: BLE001
        return {"name": repo["name"], "path": repo["path"], "ok": False,
                "error": str(exc), "findings": ""}
    finally:
        if _root_tok is not None and _rc is not None:
            try:
                _rc.reset_repo_root(_root_tok)
            except Exception:  # noqa: BLE001
                pass
    return {"name": repo["name"], "path": repo["path"], "ok": ok,
            "findings": findings}


def _synthesize(overall: str, results: list[dict], topics: list[str]) -> str:
    """Merge per-repo findings into ONE draft deliverable. A single LLM call
    (no tools) — the findings are already gathered. Draft-only."""
    from aiforge_core.llm.client import complete as _complete
    # Budget PER REPO so a many-repo run doesn't silently drop the tail repos'
    # findings (a flat cut lands mid-stream and omits later repos).
    per = max(2000, 46000 // max(1, len(results)))
    blocks = []
    for r in results:
        head = f"## {r['name']} ({r['path']})"
        body = (r.get("findings") or "").strip() or "_(no findings — explore failed)_"
        if len(body) > per:
            body = body[:per] + "\n\n_(…findings truncated for synthesis)_"
        blocks.append(f"{head}\n\n{body}")
    joined = "\n\n---\n\n".join(blocks)
    # Final cap CONSISTENT with the per-repo budget so EVERY repo's block is
    # represented (a flat [:48000] would drop the tail once per*N exceeds it,
    # e.g. an operator raising AIFORGE_ANALYSIS_MAX_REPOS).
    _cap = per * len(results) + 4000
    joined = joined[:_cap]
    topic_line = ("The requested topics were: " + "; ".join(topics) + ".\n"
                  if topics else "")
    convo = [{"role": "user", "content": (
        "You are synthesizing a cross-repository analysis into ONE cohesive "
        "draft document (markdown). DRAFT ONLY — do not suggest it was "
        "published anywhere.\n\n"
        f"Original request: {overall.strip()[:1000]}\n{topic_line}\n"
        "Below are per-repository findings. Merge them into a single, well-"
        "structured document: an executive summary, then a section per topic "
        "that compares/contrasts across the repos (cite repo + path:line), then "
        "a short 'gaps & recommendations' section. Keep concrete file references. "
        "This is a DRAFT for the user to review — do not add a 'published to "
        "Confluence' note.\n\n"
        f"=== PER-REPOSITORY FINDINGS ===\n\n{joined}")}]
    try:
        out = _complete("researcher", convo)
        return (out or "").strip() or joined
    except Exception as exc:  # noqa: BLE001 — never lose the raw findings
        _log.warning("analysis synthesize failed: %s", exc)
        return "# Analysis (raw findings — synthesis failed)\n\n" + joined


def stream_analysis_team(prompt: str, cwd: str, session_id=None,
                         repos: list[dict] | None = None,
                         topics: list[str] | None = None):
    """Generator of chat SSE events: fan out read-only explore agents (one per
    repo, in parallel), then synthesize a single draft. Coarse-grained status
    (per-repo start/finish) — the heavy per-agent trace stays inside each
    autonomous sub-run."""
    from aiforge_core.runtime import chat_cancel
    if repos is None or topics is None:
        _fan, repos, topics = should_fan_out(prompt, cwd)
    repos = repos or []
    topics = topics or []

    yield {"type": "thought", "role": "router",
           "text": (f"Analysis fan-out — {len(repos)} repo(s)"
                    + (f", topics: {', '.join(topics)}" if topics else "")
                    + ". Exploring each in parallel (read-only), then "
                    "synthesizing one draft.")}
    yield {"type": "subtasks", "items": [
        {"slug": r["name"], "goal": f"explore {r['name']}"
         + (f" for {', '.join(topics)}" if topics else ""), "status": "pending"}
        for r in repos]}

    results: list[dict] = []
    cancelled = False
    workers = min(_max_workers(), max(1, len(repos)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        fut_map = {ex.submit(_explore_one, r, topics, prompt): r for r in repos}
        for fut in concurrent.futures.as_completed(fut_map):
            r = fut_map[fut]
            if session_id is not None and chat_cancel.is_cancelled(session_id):
                cancelled = True
                # Do NOT yield an intermediate event here: the SSE producer
                # breaks its consume loop the instant it sees a post-cancel
                # event, which would abandon THIS generator before the partial
                # draft below is synthesized. Break silently so the draft
                # message (with its "Stopped early" banner) is the FIRST event
                # after cancel — the producer captures it as final_text, then
                # stops. (in-flight explores can't be interrupted — session_id
                # is None — so the pool's context-exit waits for them.)
                break
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                res = {"name": r["name"], "path": r["path"], "ok": False,
                       "error": str(exc), "findings": ""}
            results.append(res)
            # subtask_update is the event type BOTH the API state and the
            # frontend reducer handle (subtask_status was invented and dropped
            # silently, freezing the panel at "pending").
            yield {"type": "subtask_update", "slug": r["name"],
                   "status": "done" if res.get("ok") else "failed"}
            _why = res.get("error") or "no findings produced"
            yield {"type": "thought", "role": "researcher",
                   "text": (f"{r['name']}: "
                            + ("findings ready" if res.get("ok")
                               else f"explore failed ({_why})"))}

    if not results:
        yield {"type": "message",
               "text": ("Stopped before any repository was analyzed."
                        if cancelled else "No repositories could be analyzed.")}
        yield {"type": "done"}
        return

    yield {"type": "thought", "role": "synthesizer",
           "text": "Merging per-repo findings into one draft document."}
    draft = _synthesize(prompt, results, topics)
    if cancelled:
        draft = ("> ⚠ **Stopped early** — this is a PARTIAL analysis of "
                 f"{len(results)} of {len(repos)} repositories.\n\n") + draft
    yield {"type": "message", "text": draft}
    yield {"type": "done"}


__all__ = ["identify_repos", "extract_topics", "should_fan_out",
           "stream_analysis_team"]
