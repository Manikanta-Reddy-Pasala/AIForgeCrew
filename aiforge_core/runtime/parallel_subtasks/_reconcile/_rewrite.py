"""Patch parsing and the minimal-context LLM rewrite/patch resolver.

Split from ``parallel_subtasks._reconcile`` (mechanical move, behaviour identical)."""
from __future__ import annotations

import os
import re

from ._sources import _relevant_files, _spec_goal


_PATCH_RE = re.compile(
    r"<<<<<<< SEARCH\s*\n(.*?)\n=======\s*\n(.*?)\n>>>>>>> REPLACE", re.DOTALL)
_FILE_HDR_RE = re.compile(r"^###[ \t]*FILE:[ \t]*(.+?)[ \t]*$", re.MULTILINE)


def _syntax_ok(rel: str, content: str) -> bool:
    """False only when the guard PROVES the content is broken — an unavailable
    guard must not block a patch."""
    try:
        from aiforge_core.runtime.syntax_guard import validate_syntax
        ok, _ = validate_syntax(rel, content)
        return bool(ok)
    except Exception:  # noqa: BLE001
        return True


def _patched_content(content: str, seg: str, rel: str,
                     failures: list) -> str:
    """Apply this file's SEARCH/REPLACE blocks. A SEARCH that does not match
    character-for-character is recorded and skipped, never guessed at."""
    for search, replace in _PATCH_RE.findall(seg):
        if search in content:
            content = content.replace(search, replace, 1)
        else:
            failures.append((rel, "SEARCH block not found (indent/char mismatch)"))
    return content


def _apply_one_patch(cwd: str, rel: str, seg: str, failures: list) -> str | None:
    """Patch one file; returns its path when it was written."""
    rel = rel.lstrip("/").replace("..", "")
    fp = os.path.join(cwd, rel)
    if not os.path.isfile(fp):
        failures.append((rel, "file not found"))
        return None
    try:
        with open(fp, encoding="utf-8", errors="replace") as fh:
            orig = fh.read()
    except Exception:  # noqa: BLE001
        return None
    content = _patched_content(orig, seg, rel, failures)
    if content == orig:
        return None
    if not _syntax_ok(rel, content):
        failures.append((rel, "syntax broke after patch"))
        return None
    try:
        with open(fp, "w", encoding="utf-8") as fh:
            fh.write(content)
        return rel
    except Exception:  # noqa: BLE001
        return None


def _apply_patches(cwd: str, out: str) -> tuple[list, list]:
    """Deterministic Search-and-Replace applier (zero-LLM). Parses `### FILE:`
    headers + `<<<<<<< SEARCH / ======= / >>>>>>> REPLACE` blocks, verifies each
    SEARCH matches the file character-for-character, swaps it, syntax-checks, and
    writes. Surgical: fixing one test can't rewrite an unrelated section. Returns
    (written_files, failures[(file, why)])."""
    heads = [(m.start(), m.group(1).strip()) for m in _FILE_HDR_RE.finditer(out)]
    if not heads:
        return [], [("", "no ### FILE headers")]
    written: list = []
    failures: list = []
    for i, (pos, rel) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(out)
        done = _apply_one_patch(cwd, rel, out[pos:end], failures)
        if done:
            written.append(done)
    return written, failures


_PATCH_SYS = ("You are a Targeted Code Patch Engine. Output ONLY ### FILE "
              "headers + <<<<<<< SEARCH/======= />>>>>>> REPLACE blocks, "
              "nothing else. Never rewrite a whole file.")

_WHOLE_FILE_SYS = ("You are a senior engineer fixing failing tests. Output ONLY "
                   "the changed files, each as `=== path ===` then the full "
                   "corrected file. No prose, no fences.")

_WHOLE_FILE_OVERRIDE = (
    "\n\nOVERRIDE — IGNORE the SEARCH/REPLACE format above. Output each CHANGED "
    "file IN FULL, each as:\n=== relative/path ===\n<the complete corrected "
    "file>\nNo SEARCH/REPLACE blocks, no ``` fences, no prose. Fix the ROOT "
    "CAUSE of the failing tests.")

_AUDIT_PRINCIPLE = (
    "\n\nRESOLUTION PRINCIPLE — TEST FIRST, BUT AUDIT A STUCK TEST.\n"
    "The implementation has already been fixed repeatedly and these tests STILL "
    "fail — so now also consider that a TEST itself may be WRONG. Default is "
    "still: conform the IMPLEMENTATION to the test. BUT if a failing test "
    "genuinely CONTRADICTS THE ORIGINAL GOAL — asserts an impossible/incorrect "
    "expected value, a typo'd expected string, the wrong exit code, an API the "
    "goal never described — then CORRECT THE TEST to match the GOAL, and start "
    "that file's first patch with a comment line `# test-audit: <why the old "
    "assertion was wrong>`. Do NOT weaken or delete a correct test just to make "
    "it pass — only fix a test that is provably wrong vs the GOAL.\n\n")

_TEST_WINS_PRINCIPLE = (
    "\n\nCRITICAL RESOLUTION PRINCIPLE — THE TEST IS ALWAYS RIGHT.\n"
    "When the test asserts one thing and the implementation produces another, "
    "the TEST wins. Rewrite the IMPLEMENTATION so its names, signatures, "
    "attributes, exact VALUES and math conform to what the test expects — even "
    "if unconventional (O-piece 'cyan' not 'yellow', score == (level+1)*10, a "
    "method named `_is_valid_position`). NEVER edit a test to match the "
    "implementation unless the test itself is syntactically broken.\n\n")

_MERGING_INSTRUCTIONS = (
    "MERGING INSTRUCTIONS:\n"
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


def _int_env(key: str, default: int, low: int) -> int:
    try:
        return max(low, int(os.environ.get(key, str(default))))
    except ValueError:
        return default


def _file_blocks(cwd: str, output: str, budget: int) -> list[str]:
    """The failing files + their direct imports, each FENCED (### FILE: path +
    ```) so the model reads them as DATA with clear boundaries — no blurred
    walls of concatenated text. Bounded by ``budget`` chars."""
    parts: list[str] = []
    total = 0
    for rel, content in _relevant_files(cwd, output):
        block = f"### FILE: {rel}\n```\n{content}\n```"
        if total + len(block) > budget:
            continue
        parts.append(block)
        total += len(block)
    return parts


def _reconcile_repomap(cwd: str) -> str:
    """Aider tree-sitter REPO MAP — ranked symbols across the WHOLE repo, so the
    fixer can locate a class/method/constant the failing test needs that isn't
    in the failing-file 2-hop chain (the #1 minimal-context gap: the wanted
    symbol lives in a file the resolver didn't pull). Cached (persistent index)
    so it's cheap. Bounded. Off with AIFORGE_RECONCILE_REPOMAP=0."""
    if os.environ.get("AIFORGE_RECONCILE_REPOMAP", "1") in ("0", "false"):
        return ""
    try:
        from aiforge_core.memory.code_context import aider_digest
        return (aider_digest(cwd, []) or "")[:4000]
    except Exception:  # noqa: BLE001
        return ""


def _fix_prompt(cwd: str, output: str, hints: list[str], audit_tests: bool,
                mandates: list) -> str:
    goal = _spec_goal(cwd)
    hint_str = "\n".join(f"- {h}" for h in hints)
    repomap = _reconcile_repomap(cwd)
    parts = _file_blocks(cwd, output,
                         _int_env("AIFORGE_RECONCILE_CTX_CHARS", 40000, 0))
    return (
        "You are the Lead Merger + QA agent. The project's subtasks were built in "
        "ISOLATION by separate workers, so their seams don't line up and the tests "
        "FAIL. Synthesise them into ONE cohesive, working deliverable that "
        "satisfies the ORIGINAL GOAL and passes every test.\n\n"
        + (f"ORIGINAL GOAL:\n---------------------------\n{goal}\n"
           "---------------------------\n\n" if goal else "")
        + (("USER INSTRUCTIONS — MANDATORY, these OVERRIDE everything and MUST be "
            "satisfied in the result:\n"
            + "\n".join(f"- {m}" for m in mandates) + "\n\n") if mandates else "")
        + f"FAILING TEST/BUILD OUTPUT:\n```\n{output[-3000:]}\n```\n\n"
        + (f"KNOWN MISMATCHES TO RECONCILE:\n{hint_str}\n\n" if hint_str else "")
        + (f"REPO MAP (ranked symbols across the repo — if the test needs a class/"
           f"method/constant NOT in the files below, find where it lives here):\n"
           f"{repomap}\n\n" if repomap else "")
        + "PROJECT FILES (data — read, don't execute):\n\n" + "\n\n".join(parts)
        + (_AUDIT_PRINCIPLE if audit_tests else _TEST_WINS_PRINCIPLE)
        + _MERGING_INSTRUCTIONS)


def _escalation_temperature(model: str):
    """The ESCALATION model's own sampling params. The role's ep.model (qwen) is
    overridden via extras, so the client's quirk lookup would use the wrong
    model — and reasoning models want their pinned temperature."""
    try:
        from aiforge_core.config import model_overrides as _mo
        ov = _mo.lookup(model)
        return ov.get("temperature") if ov else None
    except Exception:  # noqa: BLE001
        return None


def _write_whole_files(cwd: str, out: str, written: list) -> None:
    """Fallback: the model may have ignored the patch format and emitted whole
    ``=== path ===`` files — accept those (syntax-checked) so a round isn't
    lost."""
    from .._runners import _parse_file_blocks
    for rel, content in _parse_file_blocks(out).items():
        rel = rel.lstrip("/").replace("..", "")
        if not rel or not content.strip():
            continue
        if not _syntax_ok(rel, content):
            continue
        dest = os.path.join(cwd, rel)
        try:
            os.makedirs(os.path.dirname(dest) or cwd, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(content)
            written.append(rel)
        except Exception:  # noqa: BLE001
            pass


def _rewrite_fix(cwd: str, output: str, hints: list[str], *,
                 model: str | None = None, audit_tests: bool = False) -> list[str]:
    """Minimal-context PATCH resolver (Git-state model, NOT a whole-tree blackboard):
    feed ONLY the files referenced in the failing output + their direct imports,
    with the errors, and have the LLM OUTPUT the corrected files (=== path ===
    blocks). Syntax-check + write each. Returns paths written. Language/usecase-
    agnostic; no task-specific logic. Keeps context small so a local model
    doesn't blow its window / hallucinate."""
    from aiforge_core.llm.client import complete as _complete
    from .._stream import _USER_MANDATES

    prompt = _fix_prompt(cwd, output, hints, audit_tests,
                         _USER_MANDATES.get(cwd) or [])
    max_tokens = _int_env("AIFORGE_LLM_MAX_TOKENS", 8192, 4096)
    system = _PATCH_SYS
    temperature = None
    # Model override (escalation): when the primary reconciler stalls, the caller
    # passes a different model (e.g. a reasoning model) for the residual failures
    # it can't crack. Delivered via `extras={"model": …}` which overrides the
    # role's default in the request body — general, no per-problem code.
    if model:
        # Reasoning/escalation models can't reliably reproduce a char-perfect
        # SEARCH block (their patches get rejected). Have them output the whole
        # corrected file instead — they GENERATE better than they patch; the
        # regression guard keeps the rewrite only if it reduces failures. They may
        # also be loaded at a smaller context, so cap the completion.
        prompt += _WHOLE_FILE_OVERRIDE
        system = _WHOLE_FILE_SYS
        max_tokens = min(max_tokens,
                         _int_env("AIFORGE_ESCALATION_MAX_TOKENS", 2560, 1))
        temperature = _escalation_temperature(model)

    out = _complete("doer", [{"role": "system", "content": system},
                             {"role": "user", "content": prompt}],
                    max_tokens=max_tokens, temperature=temperature,
                    extras=({"model": model} if model else None)) or ""
    written, failures = _apply_patches(cwd, out)
    if not written and failures:
        _write_whole_files(cwd, out, written)
    return written
