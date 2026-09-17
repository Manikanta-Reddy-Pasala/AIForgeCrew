"""Which repos and topics an analysis request names, and whether to fan out."""
from __future__ import annotations

import os
import re


def _pkg():
    """``analysis_pipeline``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``analysis_pipeline``; patch any other
    name on this module."""
    import aiforge_core.runtime.analysis_pipeline as package
    return package


def _is_git_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


# Common words that are ALSO real repo names (web/api/pos/core/bot/...) —
# matching them in prose spuriously pulls a repo in. Require a specific name.
_COMMON_REPO_WORDS = {"web", "api", "app", "pos", "core", "bot", "cli", "crud",
                      "ui", "db", "lib", "docs", "server", "client", "code",
                      "main", "test"}


def _repo_named_in(prompt_low: str, name: str) -> bool:
    """Does the prompt name this repo?

    A SPECIFIC name (len>=4, not a common word) is a real signal on a plain
    mention. A common/short name (core/web/pos/erp) matches too much in prose,
    so it needs a repo CUE — backticks around it, or the word
    "repo"/"repository" adjacent — so an EXPLICITLY named short repo ("the
    `core` repo") is still recovered.
    """
    esc = re.escape(name)
    if (len(name) >= 4 and name not in _COMMON_REPO_WORDS
            and re.search(r"\b" + esc + r"\b", prompt_low)):
        return True
    return bool(re.search(r"`\s*" + esc + r"\s*`", prompt_low)
                or re.search(r"\b" + esc + r"\b[\s\w]{0,12}\brepo", prompt_low)
                or re.search(r"\brepo(?:sitor(?:y|ies))?\b[\s\w]{0,12}\b"
                             + esc + r"\b", prompt_low))


def _registry_repos(prompt_low: str, add) -> None:
    """Source 1: registry names (repos.json) mentioned in the prompt."""
    try:
        from aiforge_core.config import repo_map as _rm
        paths = (_rm.list_all() or {}).get("paths") or {}
    except Exception:  # noqa: BLE001 — registry optional
        return
    for name, path in paths.items():
        nlow = str(name).strip().lower()
        if nlow and _repo_named_in(prompt_low, nlow):
            add(str(name), str(path))


def _prompt_path_repos(prompt: str, add) -> None:
    """Source 2: explicit filesystem paths in the prompt — ONLY if they are git
    repos (an analysis targets repos, not an incidental /etc/nginx mention)."""
    for m in re.findall(r"[~/][\w./\-]+", prompt):
        ap = os.path.abspath(os.path.expanduser(m.rstrip("/")))
        if _is_git_repo(ap):
            add(os.path.basename(ap), ap)


def _child_repos(cwd: str, add) -> None:
    """Source 3: child git repos of a pinned PARENT."""
    try:
        if not os.path.isdir(cwd):
            return
        for entry in sorted(os.listdir(cwd)):
            child = os.path.join(cwd, entry)
            if _is_git_repo(child):
                add(entry, child)
    except Exception:  # noqa: BLE001
        pass


def _disambiguate_names(repos: list[dict]) -> None:
    """Two repos both named `api` collide as subtask slugs, which makes the
    panel flip both rows together. Suffix a collision with its parent dir."""
    counts: dict[str, int] = {}
    for r in repos:
        counts[r["name"]] = counts.get(r["name"], 0) + 1
    for r in repos:
        if counts[r["name"]] > 1:
            parent = os.path.basename(os.path.dirname(r["path"])) or "?"
            r["name"] = f"{r['name']} ({parent})"


def _repo_cap() -> int:
    try:
        return max(2, int(os.environ.get("AIFORGE_ANALYSIS_MAX_REPOS", "12")))
    except (TypeError, ValueError):
        return 12


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
            found[ap] = {"name": name or os.path.basename(ap.rstrip("/")),
                         "path": ap}

    prompt = prompt or ""
    _registry_repos(prompt.lower(), _add)
    _prompt_path_repos(prompt, _add)
    if not found:
        # ONLY when the prompt named nothing specific (sources 1+2 empty).
        # Otherwise "summarize repoA" in a parent holding 10 checkouts would
        # fan out over all 10.
        _child_repos(cwd, _add)
    if not found:
        # 4. fallback — the pinned folder itself
        return [{"name": os.path.basename(os.path.abspath(cwd).rstrip("/"))
                 or "repo", "path": os.path.abspath(cwd)}]

    out = list(found.values())
    cap = _repo_cap()
    if len(out) > cap:
        _pkg()._log.warning("identify_repos: capped %d repos to %d (set "
                     "AIFORGE_ANALYSIS_MAX_REPOS)", len(out), cap)
        out = out[:cap]
    _disambiguate_names(out)
    return out


def _split_topics(tail: str) -> list[str]:
    """Split a phrase on commas, slashes and the words "and"/"or".

    Two replaces and one split, rather than a quantified alternation over user
    text — the denial-of-service shape a scanner asks about. Same separators,
    one pass, and the surrounding whitespace is stripped by the caller anyway.
    """
    text = (tail or "").replace(",", "\x00").replace("/", "\x00")
    words = text.split()
    out, current = [], []
    for word in words:
        if word.lower() in ("and", "or"):
            out.append(" ".join(current))
            current = []
            continue
        current.append(word)
    out.append(" ".join(current))
    return [piece for chunk in out for piece in chunk.split("\x00")]


def extract_topics(prompt: str) -> list[str]:
    """Best-effort topic list from the prompt (heuristic, no LLM).

    Looks for an explicit enumeration after a cue word ('topics', 'explore',
    'analyze', 'on', 'about', 'cover', 'including') and splits it on commas /
    'and'. Returns [] when none is found — the explore agent then does a
    general overview."""
    # Strip filesystem paths first — otherwise "analyze /home/ai/codeRepo/X and
    # /home/ai/codeRepo/Y" turns the path segments ("home", "codeRepo", the repo
    # names) into bogus "topics".
    p = re.sub(r"[~/][\w./\-]+", " ", prompt or "")
    m = re.search(
        r"\b(?:topics?|explore|analy[sz]e|cover(?:ing)?|about|on|including|"
        r"focus(?:ing)?\s+on)\b[:\s]+(.{3,240})", p, re.IGNORECASE)
    if not m:
        return []
    tail = m.group(1)
    # stop at a sentence boundary / deliverable clause
    tail = re.split(r"(?:\band\s+(?:create|write|make|produce|generate)\b|"
                    r"[.;\n]|\bthen\b)", tail, maxsplit=1)[0]
    parts = _split_topics(tail)
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
    pkg = _pkg()
    repos = pkg.identify_repos(prompt, cwd)
    topics = pkg.extract_topics(prompt)
    return (len(repos) >= 2, repos, topics)
