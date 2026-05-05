"""Learner — ADK after_model + after_tool callback. Online + offline modes."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.agents.base import BaseArchetype
from aiforge_core.agents.registry import register

@register("learner")
@dataclass
class Learner(BaseArchetype):
    name: str = "learner"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        """Online learner — distil one episodic + procedural row from
        the run's artifacts, write to Postgres. No LLM yet — heuristic.
        Skill promotion is offline (separate cron, P2).
        """
        from aiforge_core.memory import online_learner as online

        ticket_id = ctx.get("ticket_id", self.ticket_id)
        repo = ctx.get("repo", self.repo)
        plan = ctx.get("plan") or {}
        verdict = ctx.get("verifier_verdict") or {}
        grounding = ctx.get("grounding") or {}
        doer_out = ctx.get("doer_outcome") or {}
        validation = ctx.get("validation") or {}
        review = ctx.get("review") or {}

        # task_class = feature dir name (second-to-last segment), or
        # the file basename for top-level targets (e.g. README.md), or
        # "unknown" when there is no target at all.
        steps = plan.get("steps") or []
        target = doer_out.get("target") or ""
        if target:
            parts = [p for p in target.split("/") if p]
            task_class = parts[-2] if len(parts) >= 2 else parts[-1]
        else:
            task_class = "unknown"
        task_class = task_class or "unknown"

        tool_sequence = [s.get("action", "") for s in steps if s.get("action")]
        # Authoritative success signals — Architect approval is final.
        # Verifier `pass` is rare on local-LLM stack (often falls back to
        # `repair` on JSON truncation); not a hard requirement for success
        # so long as validation/review both clear.
        success = (
            grounding.get("resolved", False)
            and validation.get("decision") == "approve"
            and review.get("decision") == "approve"
        )

        outcome = "success" if success else (
            "blocked" if grounding.get("unresolved_refs")
            else "rejected"
        )
        summary = (
            f"plan_steps={len(steps)} verdict={verdict.get('verdict','?')} "
            f"grounded={grounding.get('resolved',False)} "
            f"validation={validation.get('decision','?')} "
            f"detectors={len(doer_out.get('problems') or [])}"
        )

        artifacts = {
            "plan_steps":     len(steps),
            "verdict":        verdict.get("verdict"),
            "grounded":       grounding.get("resolved"),
            "unresolved":     len(grounding.get("unresolved_refs") or []),
            "doer_problems":  len(doer_out.get("problems") or []),
            "validation":     validation.get("decision"),
        }

        online.record_episodic(
            ticket_id=ticket_id, stage="full_run", agent_role="learner",
            outcome=outcome, summary=summary, artifacts=artifacts,
        )
        online.update_procedural(
            agent_role="planner", task_class=task_class,
            tool_sequence=tool_sequence, success=success,
        )

        # Skill promotion: when this run succeeded, distill the recipe
        # so future similar tickets can recall it. Skill name = first
        # action of plan + task_class. Body = compact tool sequence.
        if success and tool_sequence:
            skill_name = f"{tool_sequence[0]}_{task_class}"[:60]
            skill_summary = (
                f"Tool sequence {tool_sequence} succeeded for "
                f"task_class={task_class} (architect-approved)."
            )
            skill_body = (
                f"## Task class\n`{task_class}`\n\n"
                f"## Tool sequence\n{tool_sequence}\n\n"
                f"## Plan steps\n{len(steps)}\n\n"
                f"## Outcome\n- detectors: {len(doer_out.get('problems') or [])}\n"
                f"- target: `{doer_out.get('target','?')}`\n"
            )
            online.promote_skill(
                repo=repo, task_class=task_class, name=skill_name,
                summary=skill_summary, body_md=skill_body,
                success=True,
            )
        elif tool_sequence:
            # Track failures too so net-success ranking is meaningful.
            online.promote_skill(
                repo=repo, task_class=task_class,
                name=f"{tool_sequence[0]}_{task_class}"[:60],
                summary=f"Failed run: {outcome}",
                body_md="(failure recorded)",
                success=False,
            )

        # Auto-correct: distill every failure mode into a recallable
        # lesson. Future tickets of the same task_class will see these
        # in their Planner/Doer prompts.
        for prob in (doer_out.get("problems") or []):
            mode = str(prob.get("mode", ""))
            evid = str(prob.get("evidence", ""))[:200]
            lesson = ""
            if mode == "F-001":
                lesson = (
                    f"Earlier ticket emitted import `{evid}` that did not "
                    "exist. Restrict imports to stdlib + plan_create_fqns "
                    "+ existing graph classes; never invent sub-packages."
                )
            elif mode == "F-003":
                lesson = (
                    "Diff context lines did not match target file. "
                    "Quote source verbatim from the supplied file body."
                )
            online.record_failure(
                repo=repo, task_class=task_class,
                mode=mode, evidence=evid, lesson=lesson,
            )
        # Apply errors are also failures worth memorising
        ae = doer_out.get("apply_error") or ""
        if ae:
            mode = ae.split(":", 1)[0].strip()
            online.record_failure(
                repo=repo, task_class=task_class, mode=mode,
                evidence=ae[:200],
                lesson=(
                    "Doer udiff failed to apply. Match the exact udiff "
                    "format: `--- /dev/null` for new files, accurate "
                    "@@ hunk counts (recount-friendly), no truncation."
                ),
            )

        # On rejected outcomes, write a failures_summary.json artifact to the
        # run dir so the Planner can recall it on future tickets.
        if outcome == "rejected":
            import glob as _glob
            import json as _json
            import os as _os
            run_dir = _os.path.expanduser(f"~/.aiforge/runs/{ticket_id}")
            try:
                _os.makedirs(run_dir, exist_ok=True)
                summary_path = _os.path.join(run_dir, "failures_summary.json")
                failure_data = {
                    "ticket_id": ticket_id,
                    "repo": repo,
                    "task_class": task_class,
                    "outcome": outcome,
                    "summary": summary,
                    "doer_problems": [
                        {"mode": p.get("mode"), "evidence": p.get("evidence", "")[:200]}
                        for p in (doer_out.get("problems") or [])
                    ],
                    "apply_error": doer_out.get("apply_error") or "",
                    "validation": validation.get("decision"),
                    "review": review.get("decision"),
                }
                with open(summary_path, "w") as _f:
                    _json.dump(failure_data, _f, indent=2)
            except Exception:
                pass  # best-effort; don't break the Learner run

        return {
            "artifact_type": "learning",
            "outcome": outcome,
            "task_class": task_class,
            "tool_sequence": tool_sequence,
            "summary": summary,
        }


def recent_failure_hints(repo: str, k: int = 5) -> list[str]:
    """Read the last k failures_summary.json files for this repo and return
    their summary strings. Called by the orchestrator to augment Planner prompts
    with ticket-level failure context that Postgres may not yet have indexed.
    """
    import glob
    import json
    import os
    pattern = os.path.expanduser("~/.aiforge/runs/*/failures_summary.json")
    rows: list[str] = []
    for p in sorted(glob.glob(pattern), reverse=True):
        try:
            with open(p) as f:
                d = json.load(f)
            if d.get("repo") == repo:
                rows.append(d.get("summary", ""))
                if len(rows) >= k:
                    break
        except Exception:
            pass
    return rows
