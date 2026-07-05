"""Cross-model review gates for the build pipeline.

Two checkpoints catch bad inputs before they waste a build:

- :func:`review_spec`  — before any code is written: catch a contradictory,
  ambiguous or scope-crept spec and refine it.
- :func:`review_tests` — after the tests are written, before the implementation:
  fix provably-wrong tests (contradictory/impossible assertions, scope creep) so
  the impl targets clean tests instead of the reconcile burning rounds on
  impossible-to-satisfy assertions.

Both are reviewed by a DIFFERENT model than the doer that wrote them — a model
can't reliably catch its own subtle bugs (e.g. an LRU eviction-order mistake).
The reviewer is auto-selected by :func:`pick_reviewer_model`.

Design note — qwen-coder on the mlx stack returns an EMPTY response when the
instruction is a *system* message, and stays silent on a *sound* artifact. We
turn both quirks into signals: one plain user turn, and empty/``CLEAN`` == sound.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

_OFF = ("0", "false")
_SKIP_DIRS = {"node_modules", "venv", ".venv", "__pycache__", "target", "build",
              "dist", ".git", ".aiforge-venv", "site-packages"}
_TEST_FILE_RE = re.compile(
    r"(^|/)(test_[^/]+|[^/]+_test|.+[Tt]est)\.(py|java|js|ts|go)$")
_TESTS_BUDGET = 24000
_reviewer_cache: dict = {}


def _disabled(flag: str, default: str = "1") -> bool:
    return os.environ.get(flag, default) in _OFF


# ── reviewer model selection ───────────────────────────────────────────────

def _is_reasoning_model(model_id: str) -> bool:
    """True if the registry classifies this model as thinking/reasoning. No
    hardcoded names here — capability detection lives in model_registry."""
    try:
        from aiforge_core.config import model_registry
        return bool(model_registry.detect_capability(model_id, "thinking"))
    except Exception:  # noqa: BLE001
        return False


def pick_reviewer_model() -> str | None:
    """The model that REVIEWS specs/tests — deliberately DIFFERENT from the doer
    that wrote them. Cross-model is the DEFAULT for every build:
    ``AIFORGE_REVIEW_MODEL`` → ``AIFORGE_ESCALATION_MODEL`` → auto-pick a loaded
    model that differs from the doer → None (single model → fall back to the
    doer). ``AIFORGE_REVIEW_CROSS_MODEL=0`` forces same-model."""
    explicit = (os.environ.get("AIFORGE_REVIEW_MODEL", "").strip()
                or os.environ.get("AIFORGE_ESCALATION_MODEL", "").strip())
    if explicit:
        return explicit
    if _disabled("AIFORGE_REVIEW_CROSS_MODEL"):
        return None
    return _auto_pick_reviewer()


def _auto_pick_reviewer() -> str | None:
    """Pick a loaded model that DIFFERS from the doer (preferring a reasoning
    model). Cached. None if only one model is loaded or the probe fails."""
    if "model" in _reviewer_cache:
        return _reviewer_cache["model"]
    picked = None
    try:
        from aiforge_core.llm.router import resolve
        endpoint = resolve("doer")
        doer_model = (getattr(endpoint, "model", "") or "").lower()
        base = (getattr(endpoint, "base_url", "") or "").rstrip("/")
        req = urllib.request.Request(
            f"{base}/models", method="GET",
            headers={"Authorization":
                     f"Bearer {getattr(endpoint, 'api_key', '') or 'na'}"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace")).get("data", [])
        loaded = [d.get("id", "") for d in data
                  if isinstance(d, dict) and d.get("id")]
        others = [m for m in loaded if m.lower() != doer_model
                  and not any(x in m.lower() for x in ("embed", "rerank"))]
        picked = (next((m for m in others if _is_reasoning_model(m)), None)
                  or (others[0] if others else None))
    except Exception:  # noqa: BLE001
        picked = None
    _reviewer_cache["model"] = picked
    return picked


def review_once(prompt: str, max_tokens: int) -> str | None:
    """One review call as a single user turn, routed to the reviewer model when
    one is configured. No retry — an empty response is the 'nothing to fix'
    signal. Returns the raw text (or None on error)."""
    reviewer = pick_reviewer_model()
    extras = {"model": reviewer} if reviewer else None
    if reviewer:                       # a reasoning reviewer THINKS before it emits
        max_tokens = max(max_tokens, 4096)
    try:
        from aiforge_core.llm.client import complete
        return complete("doer", [{"role": "user", "content": prompt}],
                        max_tokens=max_tokens, temperature=0.3, extras=extras)
    except Exception:  # noqa: BLE001
        return None


# ── the gates ──────────────────────────────────────────────────────────────

def review_spec(request: str, spec_md: str) -> tuple[str, str]:
    """Review the spec before any code is built. Returns ``(spec, note)``: the
    original spec + a 'sound' note when clean, or the refined spec + a 'refined'
    note. Soft — returns the original on any failure. Off with
    ``AIFORGE_REVIEW_SPEC=0``."""
    if _disabled("AIFORGE_REVIEW_SPEC") or not spec_md.strip():
        return spec_md, ""
    instr = ("Review this build spec against the request for contradictions, "
             "ambiguity, missing edge cases, and scope creep. If it is sound, "
             "reply with the single word CLEAN. If not, output ONLY the corrected "
             "full spec in markdown (no fences, no prose).")
    out = (review_once(f"{instr}\n\n---\n\nREQUEST:\n{request[:2000]}\n\n"
                       f"SPEC:\n{spec_md[:6000]}", 4096) or "").strip()
    if not out or out.upper().startswith("CLEAN") or len(out) < 60:
        return spec_md, "spec reviewed — sound"
    return out, "spec reviewed + refined (contradictions/ambiguity/scope)"


_CODE_SUFFIXES = (".py", ".java", ".js", ".ts", ".tsx", ".go", ".kt", ".rs",
                  ".rb", ".php", ".c", ".cpp", ".cs")


def review_plan(request: str, subs: list) -> tuple[list, str]:
    """Review the file-PLAN (manifest) before any code is built — catch filename
    TYPOS (kvdakade→kvfacade), near-duplicate modules, missing files, and scope
    creep, which a patch-based reconcile can't fix later. Returns
    ``(subs, note)``. The reviewer (a different model) reads the manifest and
    returns a corrected ``path | goal`` list, or CLEAN. Soft: any parse/sanity
    failure keeps the original. Off with ``AIFORGE_REVIEW_PLAN=0``."""
    if _disabled("AIFORGE_REVIEW_PLAN") or len(subs) < 2:
        return subs, ""
    manifest = "\n".join(f"{i + 1}. {(s.get('path') or '?')} — "
                         f"{(s.get('goal') or '')[:80]}" for i, s in enumerate(subs))
    instr = ("Review this build FILE-PLAN against the request. Look for: filename "
             "TYPOS (e.g. 'kvdakade' should be 'kvfacade'), near-DUPLICATE modules "
             "that should be a single file, MISSING modules the request needs, and "
             "files NOT needed (scope creep). If the plan is coherent and complete, "
             "reply with the single word CLEAN. Otherwise output the corrected plan, "
             "ONE line per file, exactly `path | one-line goal` — minimal and "
             "coherent, no prose, no fences.")
    out = (review_once(f"{instr}\n\n---\n\nREQUEST:\n{request[:2000]}\n\n"
                       f"PLAN:\n{manifest}", 2048) or "").strip()
    if not out or out.upper().startswith("CLEAN") or "|" not in out:
        return subs, "plan reviewed — sound"
    corrected = _parse_plan(out, subs)
    # Sanity: never accept a mangled/truncated plan that dropped most files.
    if len(corrected) < max(2, (len(subs) + 1) // 2):
        return subs, "plan reviewed — sound"
    if [s.get("path") for s in corrected] == [s.get("path") for s in subs]:
        return subs, "plan reviewed — sound"
    return corrected, f"plan reviewed + fixed ({len(subs)}→{len(corrected)} files)"


def _parse_plan(out: str, subs: list) -> list:
    """Turn the reviewer's ``path | goal`` lines into subtask dicts, preserving
    each original subtask's fields (slug etc.) by closest-path match."""
    import difflib
    by_path = {(s.get("path") or "").strip(): s for s in subs}
    paths = list(by_path)
    seen, new = set(), []
    for line in out.splitlines():
        if "|" not in line:
            continue
        path, _, goal = line.partition("|")
        path = path.strip().lstrip("0123456789.) ").strip("`").strip()
        goal = goal.strip()
        if not path or not path.endswith(_CODE_SUFFIXES) or path in seen:
            continue
        seen.add(path)
        base = by_path.get(path)
        if base is None:                       # typo-renamed: match the closest old path
            m = difflib.get_close_matches(path, paths, n=1, cutoff=0.6)
            base = by_path.get(m[0]) if m else None
        s = dict(base) if base else {}
        s["path"] = path
        if goal:
            s["goal"] = goal
        if not s.get("slug"):
            s["slug"] = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")[:40] or "step"
        new.append(s)
    return new


def review_tests(cwd: str, spec_md: str) -> tuple[list[str], str]:
    """Review the written test files before the impl reconcile and fix
    provably-wrong tests. Returns ``(changed_files, note)``. Off with
    ``AIFORGE_REVIEW_TESTS=0``."""
    if _disabled("AIFORGE_REVIEW_TESTS"):
        return [], ""
    blocks = _fenced_test_blocks(cwd)
    if not blocks:
        return [], ""
    instr = ("You are auditing test files for CORRECTNESS. For EACH test, mentally "
             "EXECUTE it step by step against the spec — track the state after every "
             "call (for a cache/structure: the exact contents + recency/ordering "
             "after each get/put) — and check whether each assertion matches the "
             "state the spec dictates. A get that refreshes recency, an off-by-one "
             "eviction, a wrong expected value: these are bugs. Do NOT skim.\n"
             "If every assertion is correct, reply with the single word CLEAN. "
             "Otherwise output ONLY the corrected files, each as `=== path ===` then "
             "the full file, marking each fix with a `# test-review:` comment. Fix "
             "ONLY provably-wrong assertions; never weaken a correct test.")
    out = (review_once(f"{instr}\n\n---\n\nSPEC:\n{spec_md[:4000]}\n\nTESTS:\n\n"
                       + "\n\n".join(blocks), 8192) or "").strip()
    if not out or out.upper().startswith("CLEAN") or "=== " not in out:
        return [], "tests reviewed — sound"
    changed = _write_reviewed_files(cwd, out)
    return changed, (f"tests reviewed + fixed {len(changed)} file(s)"
                     if changed else "tests reviewed — sound")


# ── helpers ────────────────────────────────────────────────────────────────

def find_test_files(cwd: str) -> list[str]:
    """Relative paths of test files in the tree (pytest / JUnit / ``*_test.*``)."""
    out: list[str] = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), cwd)
            if _TEST_FILE_RE.search(rel.replace(os.sep, "/")):
                out.append(rel)
    return out


def _fenced_test_blocks(cwd: str) -> list[str]:
    """``### FILE: <path>`` fenced blocks of each test file, capped to a budget."""
    blocks, total = [], 0
    for rel in find_test_files(cwd):
        try:
            body = open(os.path.join(cwd, rel), encoding="utf-8",
                        errors="replace").read()
        except Exception:  # noqa: BLE001
            continue
        block = f"### FILE: {rel}\n```\n{body}\n```"
        if total + len(block) > _TESTS_BUDGET:
            break
        blocks.append(block)
        total += len(block)
    return blocks


def _write_reviewed_files(cwd: str, out: str) -> list[str]:
    """Parse the reviewer's ``=== path ===`` blocks, syntax-check, write. Returns
    the paths written."""
    from aiforge_core.runtime.parallel_subtasks import _parse_file_blocks
    changed: list[str] = []
    for rel, content in _parse_file_blocks(out).items():
        rel = rel.lstrip("/").replace("..", "")
        if not rel or not content.strip():
            continue
        try:
            from aiforge_core.runtime.syntax_guard import validate_syntax
            ok, _ = validate_syntax(rel, content)
            if not ok:
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            with open(os.path.join(cwd, rel), "w", encoding="utf-8") as fh:
                fh.write(content)
            changed.append(rel)
        except Exception:  # noqa: BLE001
            pass
    return changed
