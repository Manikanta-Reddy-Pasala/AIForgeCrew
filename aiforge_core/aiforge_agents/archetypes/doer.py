"""Doer — CRITIC loop with five layered checks."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register

def _git_apply_diff(
    *, repo_path: str, ticket_id: str, udiff: str,
) -> tuple[bool, str, str]:
    """Apply a unified diff on a fresh ticket branch.

    Returns (applied, branch_name, error). On any failure, the worktree
    is restored via `git checkout -- .` and the branch is left intact
    for inspection (no force delete).

    Rules:
    - Refuse to apply on a dirty worktree.
    - Branch name: `aiforge/<ticket-id>` (stable across CRITIC retries).
    - `git apply --check` first; only commit if check passes.
    """
    import subprocess
    from pathlib import Path

    if not Path(repo_path).is_dir():
        return False, "", "repo_path_missing"
    branch = f"aiforge/{ticket_id}"

    def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
        return subprocess.run(
            args, cwd=repo_path, capture_output=True, text=True,
            timeout=30, **kw,
        )

    # Refuse on dirty tree — but ignore our own scratch dir, which the
    # Doer itself just wrote into when it saved the proposal artifact.
    st = _run(["git", "status", "--porcelain"])
    if st.returncode != 0:
        return False, "", f"git_status: {st.stderr.strip()}"
    # Ignore our own scratch dirs + common per-machine clutter — these
    # never belong in a PR diff and shouldn't gate apply.
    _SKIP_PATTERNS = (
        ".aiforge/", ".aiforge-worktrees/",
        "graphify-out/", ".idea/", ".vscode/", ".DS_Store",
    )
    significant = [
        ln for ln in st.stdout.splitlines()
        if ln.strip()
        and not any(pat in ln for pat in _SKIP_PATTERNS)
    ]
    if significant:
        return False, "", "dirty_worktree: " + significant[0][:120]

    # Determine the upstream default branch so the FIRST apply for a
    # given ticket branches from a clean baseline (master/main).
    base = "main"
    sb = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"])
    if sb.returncode == 0:
        ref = sb.stdout.strip().split("/")[-1]
        if ref:
            base = ref

    # Branch policy:
    #   - if `aiforge/<ticket>` already exists locally, just check it
    #     out — multi-step Doer stacks commits onto the same branch.
    #   - otherwise create it from `base` so we don't inherit any
    #     stale state from another ticket.
    branch_exists = _run(
        ["git", "rev-parse", "--verify", "--quiet", branch],
    ).returncode == 0
    if branch_exists:
        sw = _run(["git", "checkout", branch])
    else:
        co_base = _run(["git", "checkout", base])
        if co_base.returncode != 0:
            pass  # fall through; next checkout surfaces the error
        sw = _run(["git", "checkout", "-B", branch, base])
    if sw.returncode != 0:
        return False, "", f"checkout: {sw.stderr.strip()}"

    # Write patch to a temp file (multi-line stdin via shell is fragile)
    patch_file = Path(repo_path) / ".aiforge" / "tmp" / f"{ticket_id}.apply.patch"
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(udiff)

    # `--recount` rebuilds incorrect hunk line counts that LLMs emit
    # routinely (off-by-one or wrong total). `--ignore-whitespace`
    # tolerates indentation drift. Reject only on real conflicts.
    apply_args = [
        "git", "apply", "--recount", "--ignore-whitespace",
        "--whitespace=nowarn",
    ]
    chk = _run([*apply_args, "--check", str(patch_file)])
    if chk.returncode != 0:
        return False, branch, f"apply_check: {chk.stderr.strip()[:300]}"

    ap = _run([*apply_args, str(patch_file)])
    if ap.returncode != 0:
        return False, branch, f"apply: {ap.stderr.strip()[:300]}"

    # Stage + commit, but never include our own scratch dirs.
    _run([
        "git", "add", "-A",
        ":(exclude).aiforge",
        ":(exclude).aiforge-worktrees",
        ":(exclude)graphify-out",
        ":(exclude).idea",
        ":(exclude).vscode",
    ])
    cm = _run([
        "git", "commit", "-m",
        f"aiforge({ticket_id}): apply Doer-generated diff",
    ])
    if cm.returncode != 0:
        # Rollback worktree, keep branch
        _run(["git", "checkout", "--", "."])
        return False, branch, f"commit: {cm.stderr.strip()[:200]}"

    return True, branch, ""


def _plan_create_fqns(plan: dict[str, Any]) -> set[str]:
    """Derive Java/Kotlin FQNs from action=create steps.

    Path `src/main/java/com/pos/backend/feature/ledger/LedgerCategoryService.java`
    → FQN `com.pos.backend.feature.ledger.LedgerCategoryService`.
    """
    out: set[str] = set()
    for step in (plan.get("steps") or []):
        if step.get("action") != "create":
            continue
        tgt = (step.get("target") or "").strip()
        if not tgt or not tgt.endswith((".java", ".kt")):
            continue
        for prefix in ("src/main/java/", "src/test/java/",
                       "src/main/kotlin/", "src/test/kotlin/"):
            i = tgt.find(prefix)
            if i != -1:
                rel = tgt[i + len(prefix):]
                fqn = rel.rsplit(".", 1)[0].replace("/", ".")
                out.add(fqn)
                break
    return out


@register("doer")
@dataclass
class Doer(BaseArchetype):
    name: str = "doer"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        """Single-pass Doer:
            1. pick first 'edit' step from plan
            2. read target file via repo path
            3. ask LLM for unified diff (grammar nudged via prompt)
            4. extract imports → HallucinatedImportDetector
            5. parse hunks → DiffContextHashDetector
            6. write a `.aiforge/proposals/<ticket>.patch` artifact
            7. (optional) apply + git diff verify if ctx['apply']=True

        No CRITIC retry loop yet — that's P2.
        """
        from pathlib import Path

        from aiforge_core.aiforge_agents.runtime import detectors, llm_client
        from aiforge_core.aiforge_agents.runtime import prompt_helpers as ph

        plan = ctx.get("plan", {})
        repo = ctx.get("repo", self.repo)
        failures_hint = ctx.get("failures_hint") or []
        repo_path = ctx.get("repo_path", "")
        ticket_id = ctx.get("ticket_id", self.ticket_id)
        apply = bool(ctx.get("apply", False))

        # Caller can pin a specific step (multi-step orchestration);
        # otherwise pick the first edit/create step in the plan.
        target_step = ctx.get("target_step")
        if target_step:
            step = target_step
        else:
            write_steps = [s for s in (plan.get("steps") or [])
                           if s.get("action") in ("edit", "create")]
            if not write_steps:
                return {"artifact_type": "doer_outcome",
                        "skipped": True, "reason": "no_write_step"}
            step = write_steps[0]
        target_rel = step.get("target", "")
        action = step.get("action", "edit")

        # Read target file for `edit`; for `create`, file_text is empty
        # and we ask LLM to generate the full file.
        file_text = ""
        if action == "edit" and repo_path and target_rel:
            try:
                file_text = (Path(repo_path) / target_rel).read_text()
            except OSError as exc:
                return {"artifact_type": "doer_outcome",
                        "skipped": True,
                        "reason": f"target_unreadable: {exc}",
                        "step_id": step.get("id"),
                        "target": target_rel}

        # Pre-compute plan-create FQN list so we can show the model
        # exactly which sibling classes it can rely on. Same set the
        # detector uses for whitelisting — keeps prompt + check aligned.
        plan_create_fqns = _plan_create_fqns(plan)
        siblings_block = ""
        if plan_create_fqns:
            siblings_block = (
                "# Sibling classes being created in this same plan — "
                "you MAY import these even though they don't yet exist in graph. "
                "Use EXACT FQNs (no sub-package guessing):\n"
                + "\n".join(f"- {fq}" for fq in sorted(plan_create_fqns))
                + "\n"
            )

        # Failures-recall (auto-correct across tickets) — same data
        # the Planner sees, also handed to Doer so it doesn't emit the
        # same hallucinated import / sub-package twice.
        failures_block = ph.render_failures_block(failures_hint)

        # CRITIC feedback block — surface prior-attempt issues so Doer
        # can fix them on retry. Caller passes previous_udiff +
        # detector_problems + architect_comments.
        previous_udiff = (ctx.get("previous_udiff") or "").strip()
        detector_problems = ctx.get("detector_problems") or []
        architect_comments = ctx.get("architect_comments") or []
        critic_block = ""
        if previous_udiff or detector_problems or architect_comments:
            lines = ["# CRITIC FEEDBACK from prior attempt — fix these:"]
            if detector_problems:
                lines.append("## Detector violations (block-class):")
                for p in detector_problems[:8]:
                    lines.append(f"- {p.get('mode')}: {p.get('evidence')}")
            if architect_comments:
                lines.append("## Architect review comments:")
                for c in architect_comments[:8]:
                    lines.append(f"- {c}")
            if previous_udiff:
                lines.append("## Your previous diff (improve, don't repeat):")
                lines.append("```diff")
                lines.append(previous_udiff[:1500])
                lines.append("```")
            lines.append("")
            critic_block = "\n".join(lines)

        # LLM — generate udiff
        if action == "create":
            system = (
                "You produce a unified diff (udiff) that CREATES a new file. "
                "Output ONE fenced block: ```diff\\n--- /dev/null\\n+++ b/<path>\\n"
                "@@ -0,0 +1,<count> @@\\n+<line>\\n+<line>\\n...\\n``` "
                "Every line of the new file is a `+` line. "
                "STRICT IMPORT RULES: only use (a) standard libraries "
                "(java.*, javax.*, jakarta.*, org.springframework.*, lombok.*, "
                "io.swagger.*, com.fasterxml.*, org.slf4j.*, org.junit.*); "
                "(b) classes from the # Sibling classes list below; "
                "(c) classes already in the same package as the target. "
                "Never invent sub-packages (e.g. `.dto.X`, `.service.X`) "
                "that are not in the sibling list. "
                "If a DTO is needed but not in the sibling list, declare "
                "it as a static nested class inside the new file instead "
                "of importing from a non-existent sub-package. "
                "Match the source language of the target path."
            )
            user = (
                f"{failures_block}"
                f"{critic_block}"
                f"# Step (create new file)\n{step}\n\n"
                f"# Target path: {target_rel}\n"
                f"{siblings_block}"
                f"# Plan context\n{plan.get('steps')}\n"
            )
        else:
            system = (
                "You produce a unified diff (udiff) that satisfies the step. "
                "Output ONE fenced block: ```diff\\n--- a/<path>\\n+++ b/<path>\\n"
                "@@ -<start>,<count> +<start>,<count> @@\\n<context/+/->...\\n``` "
                "Context lines (starting with single space) MUST exactly match "
                "lines in the supplied source. Never invent imports — only use "
                "imports already present in the file or its package, plus "
                "classes from the # Sibling classes list when shown. "
                "Keep diff minimal."
            )
            user = (
                f"{failures_block}"
                f"{critic_block}"
                f"# Step\n{step}\n\n"
                f"# Target file: {target_rel}\n"
                f"{siblings_block}"
                f"```\n{ph.compact(file_text, head=12000, tail=4000)}\n```\n"
            )
        raw = llm_client.call_text(
            model=self.model or "qwen3-coder-next",
            system=system, user=user,
            temperature=self.temperature or 0.2,
            max_tokens=self.max_tokens or 24000,
        )
        # Extract diff fenced block
        import re
        m = re.search(r"```diff\s*\n(.*?)```", raw, re.DOTALL)
        udiff = m.group(1) if m else raw

        # Truncation detection: if the closing ``` was never emitted,
        # the udiff is incomplete and any apply will fail with corrupt
        # patch. Surface it explicitly so the orchestrator's CRITIC
        # retry can see "truncated_output" and try again.
        truncated = (m is None and "```diff" in raw) or (
            m is None and raw.lstrip().startswith(("--- ", "+++ ", "@@ "))
            and not raw.rstrip().endswith(("\n", "}"))
        )

        # Detectors — plan_create_fqns already computed above for prompt
        problems: list[dict] = []

        # Optional Neo4j driver for graph-resolved imports
        try:
            import os
            from neo4j import GraphDatabase
            n4j = GraphDatabase.driver(
                os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687"),
                auth=(
                    os.environ.get("AIFORGE_NEO4J_USER", "neo4j"),
                    os.environ.get("AIFORGE_NEO4J_PASSWORD", "password"),
                ),
            )
        except Exception:
            n4j = None

        imp_det = detectors.HallucinatedImportDetector(
            repo=repo, driver=n4j,
            plan_create_fqns=plan_create_fqns,
        )
        for hit in imp_det.check(udiff):
            problems.append({"mode": hit.mode.id, "evidence": hit.evidence})
        if n4j is not None:
            try:
                n4j.close()
            except Exception:
                pass

        # Diff hash check only valid for `edit` (existing file content)
        if action == "edit" and file_text:
            hash_hit = detectors.DiffContextHashDetector.check(
                udiff=udiff, file_text=file_text,
            )
            if hash_hit:
                problems.append({"mode": hash_hit.mode.id,
                                 "evidence": hash_hit.evidence})

        # Save proposal artifact (always)
        artifact_path = ""
        if repo_path and ticket_id:
            try:
                proposals = Path(repo_path) / ".aiforge" / "proposals"
                proposals.mkdir(parents=True, exist_ok=True)
                artifact_path = str(proposals / f"{ticket_id}.patch")
                Path(artifact_path).write_text(udiff)
            except OSError:
                artifact_path = ""

        # Apply path: try `git apply --check`, then `git apply`, on a
        # dedicated ticket branch. Skip on detector hits or if caller
        # didn't request apply. Rolls back the branch on failure.
        applied = False
        applied_branch = ""
        apply_error = ""
        if truncated:
            apply_error = "truncated_output"
        elif (apply and repo_path and ticket_id
                and udiff.strip()
                and len(problems) == 0):
            applied, applied_branch, apply_error = _git_apply_diff(
                repo_path=repo_path, ticket_id=ticket_id, udiff=udiff,
            )

        return {
            "artifact_type": "doer_outcome",
            "step_id": step.get("id"),
            "action": action,
            "target": target_rel,
            "udiff": udiff[:4000],   # truncate for storage
            "problems": problems,
            "applied": applied,
            "applied_branch": applied_branch,
            "apply_error": apply_error,
            "tests_green": False,    # tests path (P3) not wired
            "artifact_path": artifact_path,
            "blocked_by_detectors": len(problems) > 0,
        }
