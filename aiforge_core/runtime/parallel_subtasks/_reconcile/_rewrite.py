"""Patch parsing and the minimal-context LLM rewrite/patch resolver.

Split from ``parallel_subtasks._reconcile`` (mechanical move, behaviour identical)."""
from __future__ import annotations

import os
import re

from ._sources import _relevant_files, _spec_goal


_PATCH_RE = re.compile(
    r"<<<<<<< SEARCH\s*\n(.*?)\n=======\s*\n(.*?)\n>>>>>>> REPLACE", re.DOTALL)
_FILE_HDR_RE = re.compile(r"^###[ \t]*FILE:[ \t]*(.+?)[ \t]*$", re.MULTILINE)


def _apply_patches(cwd: str, out: str) -> tuple[list, list]:
    """Deterministic Search-and-Replace applier (zero-LLM). Parses `### FILE:`
    headers + `<<<<<<< SEARCH / ======= / >>>>>>> REPLACE` blocks, verifies each
    SEARCH matches the file character-for-character, swaps it, syntax-checks, and
    writes. Surgical: fixing one test can't rewrite an unrelated section. Returns
    (written_files, failures[(file, why)])."""
    written: list = []
    failures: list = []
    heads = [(m.start(), m.group(1).strip()) for m in _FILE_HDR_RE.finditer(out)]
    if not heads:
        return written, [("", "no ### FILE headers")]
    for i, (pos, rel) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(out)
        seg = out[pos:end]
        rel = rel.lstrip("/").replace("..", "")
        fp = os.path.join(cwd, rel)
        if not os.path.isfile(fp):
            failures.append((rel, "file not found"))
            continue
        try:
            with open(fp, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception:  # noqa: BLE001
            continue
        orig = content
        for search, replace in _PATCH_RE.findall(seg):
            if search in content:
                content = content.replace(search, replace, 1)
            else:
                failures.append((rel, "SEARCH block not found (indent/char mismatch)"))
        if content == orig:
            continue
        try:
            from aiforge_core.runtime.syntax_guard import validate_syntax
            ok, _ = validate_syntax(rel, content)
            if not ok:
                failures.append((rel, "syntax broke after patch"))
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            with open(fp, "w", encoding="utf-8") as fh:
                fh.write(content)
            written.append(rel)
        except Exception:  # noqa: BLE001
            pass
    return written, failures


def _rewrite_fix(cwd: str, output: str, hints: list[str], *,
                 model: str | None = None, audit_tests: bool = False) -> list[str]:
    """Minimal-context PATCH resolver (Git-state model, NOT a whole-tree blackboard):
    feed ONLY the files referenced in the failing output + their direct imports,
    with the errors, and have the LLM OUTPUT the corrected files (=== path ===
    blocks). Syntax-check + write each. Returns paths written. Language/usecase-
    agnostic; no task-specific logic. Keeps context small so a local model
    doesn't blow its window / hallucinate."""
    from aiforge_core.llm.client import complete as _complete
    from .._runners import _parse_file_blocks
    from .._stream import _USER_MANDATES
    try:
        budget = int(os.environ.get("AIFORGE_RECONCILE_CTX_CHARS", "40000"))
    except ValueError:
        budget = 40000
    # Fence each file (### FILE: path + ```) so the model reads it as DATA, with
    # clear boundaries — no blurred walls of concatenated text.
    parts: list[str] = []
    total = 0
    for rel, content in _relevant_files(cwd, output):
        block = f"### FILE: {rel}\n```\n{content}\n```"
        if total + len(block) > budget:
            continue
        parts.append(block)
        total += len(block)
    hint_str = "\n".join(f"- {h}" for h in hints)
    goal = _spec_goal(cwd)
    # Aider tree-sitter REPO MAP — ranked symbols across the WHOLE repo, so the
    # fixer can locate a class/method/constant the failing test needs that isn't in
    # the failing-file 2-hop chain above (the #1 minimal-context gap: the wanted
    # symbol lives in a file the resolver didn't pull). Cached (persistent index)
    # so it's cheap. Bounded. Off with AIFORGE_RECONCILE_REPOMAP=0.
    repomap = ""
    if os.environ.get("AIFORGE_RECONCILE_REPOMAP", "1") not in ("0", "false"):
        try:
            from aiforge_core.memory.code_context import aider_digest
            repomap = (aider_digest(cwd, []) or "")[:4000]
        except Exception:  # noqa: BLE001
            repomap = ""
    prompt = (
        "You are the Lead Merger + QA agent. The project's subtasks were built in "
        "ISOLATION by separate workers, so their seams don't line up and the tests "
        "FAIL. Synthesise them into ONE cohesive, working deliverable that "
        "satisfies the ORIGINAL GOAL and passes every test.\n\n"
        + (f"ORIGINAL GOAL:\n---------------------------\n{goal}\n"
           "---------------------------\n\n" if goal else "")
        + (("USER INSTRUCTIONS — MANDATORY, these OVERRIDE everything and MUST be "
            "satisfied in the result:\n"
            + "\n".join(f"- {m}" for m in _USER_MANDATES.get(cwd, [])) + "\n\n")
           if _USER_MANDATES.get(cwd) else "")
        + f"FAILING TEST/BUILD OUTPUT:\n```\n{output[-3000:]}\n```\n\n"
        + (f"KNOWN MISMATCHES TO RECONCILE:\n{hint_str}\n\n" if hint_str else "")
        + (f"REPO MAP (ranked symbols across the repo — if the test needs a class/"
           f"method/constant NOT in the files below, find where it lives here):\n"
           f"{repomap}\n\n" if repomap else "")
        + "PROJECT FILES (data — read, don't execute):\n\n" + "\n\n".join(parts)
        + ("\n\nRESOLUTION PRINCIPLE — TEST FIRST, BUT AUDIT A STUCK TEST.\n"
           "The implementation has already been fixed repeatedly and these tests "
           "STILL fail — so now also consider that a TEST itself may be WRONG. "
           "Default is still: conform the IMPLEMENTATION to the test. BUT if a "
           "failing test genuinely CONTRADICTS THE ORIGINAL GOAL — asserts an "
           "impossible/incorrect expected value, a typo'd expected string, the "
           "wrong exit code, an API the goal never described — then CORRECT THE "
           "TEST to match the GOAL, and start that file's first patch with a "
           "comment line `# test-audit: <why the old assertion was wrong>`. Do NOT "
           "weaken or delete a correct test just to make it pass — only fix a test "
           "that is provably wrong vs the GOAL.\n\n"
           if audit_tests else
           "\n\nCRITICAL RESOLUTION PRINCIPLE — THE TEST IS ALWAYS RIGHT.\n"
           "When the test asserts one thing and the implementation produces another, "
           "the TEST wins. Rewrite the IMPLEMENTATION so its names, signatures, "
           "attributes, exact VALUES and math conform to what the test expects — "
           "even if unconventional (O-piece 'cyan' not 'yellow', score == "
           "(level+1)*10, a method named `_is_valid_position`). NEVER edit a test to "
           "match the implementation unless the test itself is syntactically broken.\n\n")
        + "MERGING INSTRUCTIONS:\n"
          "1. Re-read the ORIGINAL GOAL — the result must satisfy it.\n"
          "2. Cross-reference dependencies: align every import / class / function / "
          "constant name + signature to ONE canonical spelling — the name the TEST "
          "uses. A package __init__ / re-export must ONLY import names defined at "
          "MODULE level in the target; if a name is a class METHOD or missing, "
          "remove it from the import + __all__.\n"
          "3. Do NOT drop working code — make the MINIMAL change that satisfies the "
          "failing assertions (add the exact attribute/method the test calls, fix "
          "the value/formula the test expects).\n"
          "4. You are PROHIBITED from rewriting whole files (a full rewrite silently "
          "shifts working code and breaks other tests). Emit TARGETED "
          "Search-and-Replace PATCHES. For each file you change, output a header "
          "line `### FILE: relative/path` then one or more blocks EXACTLY:\n"
          "<<<<<<< SEARCH\n<the exact existing lines to change — character-for-"
          "character incl. indentation>\n=======\n<the corrected lines>\n"
          ">>>>>>> REPLACE\n"
          "The SEARCH text MUST appear verbatim in the current file. Keep each "
          "SEARCH block small (the few lines around the defect). Output ONLY the "
          "`### FILE:` headers + SEARCH/REPLACE blocks — no whole files, no ``` "
          "fences, no prose.")
    try:
        mt = max(4096, int(os.environ.get("AIFORGE_LLM_MAX_TOKENS", "8192")))
    except ValueError:
        mt = 8192
    # Model override (escalation): when the primary reconciler stalls, the caller
    # passes a different model (e.g. a reasoning model) for the residual failures
    # it can't crack. Delivered via `extras={"model": …}` which overrides the
    # role's default in the request body — general, no per-problem code.
    _extras = {"model": model} if model else None
    _temp = None
    _sys = ("You are a Targeted Code Patch Engine. Output ONLY ### FILE headers + "
            "<<<<<<< SEARCH/======= />>>>>>> REPLACE blocks, nothing else. Never "
            "rewrite a whole file.")
    if model:
        # Reasoning/escalation models can't reliably reproduce a char-perfect
        # SEARCH block (their patches get rejected). Have them output the whole
        # corrected file instead — they GENERATE better than they patch; the
        # regression guard keeps the rewrite only if it reduces failures.
        prompt += ("\n\nOVERRIDE — IGNORE the SEARCH/REPLACE format above. Output "
                   "each CHANGED file IN FULL, each as:\n=== relative/path ===\n"
                   "<the complete corrected file>\nNo SEARCH/REPLACE blocks, no ``` "
                   "fences, no prose. Fix the ROOT CAUSE of the failing tests.")
        _sys = ("You are a senior engineer fixing failing tests. Output ONLY the "
                "changed files, each as `=== path ===` then the full corrected "
                "file. No prose, no fences.")
    if model:
        # An escalation (reasoning) model may be loaded at a smaller context — cap
        # completion so prompt+completion fit; its fixes are targeted anyway.
        try:
            mt = min(mt, int(os.environ.get("AIFORGE_ESCALATION_MAX_TOKENS", "2560")))
        except ValueError:
            mt = 2560
        # Apply the ESCALATION model's own sampling params (the role's ep.model —
        # qwen — is overridden via extras, so the client's quirk lookup would use
        # the wrong model). Reasoning models want their pinned temperature.
        try:
            from aiforge_core.config import model_overrides as _mo
            _ov = _mo.lookup(model)
            if _ov and _ov.get("temperature") is not None:
                _temp = _ov["temperature"]
        except Exception:  # noqa: BLE001
            pass
    out = _complete("doer", [
        {"role": "system", "content": _sys},
        {"role": "user", "content": prompt}],
        max_tokens=mt, temperature=_temp, extras=_extras) or ""
    written, failures = _apply_patches(cwd, out)
    if not written and failures:
        # Fallback: the model may have ignored the patch format and emitted whole
        # `=== path ===` files — accept those (syntax-checked) so a round isn't lost.
        for rel, content in _parse_file_blocks(out).items():
            rel = rel.lstrip("/").replace("..", "")
            if not rel or not content.strip():
                continue
            try:
                from aiforge_core.runtime.syntax_guard import validate_syntax
                _ok, _ = validate_syntax(rel, content)
                if not _ok:
                    continue
            except Exception:  # noqa: BLE001
                pass
            dest = os.path.join(cwd, rel)
            try:
                os.makedirs(os.path.dirname(dest) or cwd, exist_ok=True)
                with open(dest, "w", encoding="utf-8") as fh:
                    fh.write(content)
                written.append(rel)
            except Exception:  # noqa: BLE001
                pass
    return written
