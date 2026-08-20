"""Second-agent PR review (standards gap C2).

After ``commit_push_open_pr`` succeeds, run a Reviewer pass over the
final diff and post the verdict as a PR comment. KISS: no new ADK
LlmAgent (the Refiner already does in-loop review). One blocking
LiteLLM call against the planner model is enough.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Any

log = logging.getLogger("aiforge.pr_reviewer")

_PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")

_REVIEW_PROMPT = """You are reviewing a pull request diff before merge.
Rate each axis 0-2 (0=fail, 1=concern, 2=clean) and give a one-line
rationale per axis. Axes:
- scope     — does the diff stay inside the ticket's stated scope?
- correctness — does the change actually do what the title claims?
- security  — any obvious injection / secret leak / unsafe call?
- regression  — likely test failures or behaviour shifts?
- style    — naming + comments + adherence to repo conventions?

For the correctness and regression axes, re-check each hunk
specifically for: (1) comparison/boundary operator changes,
(2) removed or weakened locking vs documented thread-safety,
(3) changed return values vs the docstring contract,
(4) swallowed exceptions. Missing a real regression is far worse
than a long review.

Return STRICT JSON: {"verdict": "approve"|"comment"|"request_changes",
"rationale": "...", "scope": int, "correctness": int, "security": int,
"regression": int, "style": int}.

Ticket title: {{title}}
Ticket body:
{{body}}

Diff (truncated to 12 000 chars):
```
{{diff}}
```
"""


def _parse_pr_url(url: str) -> tuple[str, str, str] | None:
    m = _PR_URL_RE.search(url or "")
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def _gh_pr_diff(owner: str, repo: str, num: str) -> str:
    proc = subprocess.run(
        ["gh", "pr", "diff", num, "--repo", f"{owner}/{repo}"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout[:12000]


def _gh_pr_comment(owner: str, repo: str, num: str, body: str) -> bool:
    proc = subprocess.run(
        ["gh", "pr", "comment", num, "--repo", f"{owner}/{repo}",
         "--body", body],
        capture_output=True, text=True, timeout=20,
    )
    return proc.returncode == 0


def _llm_review(prompt: str) -> dict[str, Any]:
    """Single review call via LiteLLM against the configured endpoint.

    Defaults to the local LM Studio served model; override with
    ``AIFORGE_REVIEWER_MODEL`` (e.g. ``openai/<id>`` for any
    OpenAI-compatible endpoint). Returns parsed JSON or ``{}`` on failure.
    """
    model = os.environ.get("AIFORGE_REVIEWER_MODEL", "openai/qwen3-coder-next")
    try:
        import litellm
    except ImportError:
        return {}
    base = os.environ.get("AIFORGE_LM_BASE_URL", "http://127.0.0.1:1234/v1")
    api_key = os.environ.get("AIFORGE_LM_API_KEY", "lm-studio")
    try:
        from aiforge_core.llm.user_agent import user_agent
        resp = litellm.completion(
            model=model,
            api_base=base, api_key=api_key,
            extra_headers={"User-Agent": user_agent()},
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1, timeout=120,
        )
        text = resp["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        log.warning("pr_reviewer LLM failed: %s", exc)
        return {}
    return _extract_review_json(text)


def _extract_review_json(text: str) -> dict[str, Any]:
    """First balanced ``{...}`` object → dict, ``{}`` when none parses.

    Replaces a greedy ``\\{.*\\}`` regex that mis-grabbed prose braces and
    failed on nested/trailing text. A review that no longer parses reads as
    "no findings" (a silent pass), so parse HARDER before emptying — only a
    genuinely JSON-free response yields ``{}`` (which stays fail-open, never
    fail-closed, so a flaky model can't wedge an autonomous run)."""
    try:
        from aiforge_core.runtime.rule_capture import _extract_json
        obj = _extract_json(text or "")
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------
# Iterative reviewer<->doer handshake (gap A5).
#
# Axes are scored 0-2 (0=fail, 1=concern, 2=clean); we normalize to 0-1 by
# dividing by ``_AXIS_MAX`` so the float ``min_score`` thresholds are
# meaningful. ``correctness``/``security``/``regression`` are *critical*:
# a single low critical axis forces a revision even if the average is fine.
# All three functions below are pure (no I/O) so they are trivially testable
# with fake callables; real wiring is gated behind AIFORGE_REVIEWER_ITERATE.
# --------------------------------------------------------------------------

_AXIS_MAX = 2.0
_CRITICAL_AXES = ("correctness", "security", "regression")
_ALL_AXES = ("scope", "correctness", "security", "regression", "style")


def iterate_enabled() -> bool:
    """Whether the iterative reviewer<->doer handshake is active.

    Default off (``AIFORGE_REVIEWER_ITERATE=0``) preserves the current
    single-shot ``review_pr`` behavior. Set to ``1``/``true`` to let a
    caller drive ``review_rounds`` for real.
    """
    return os.environ.get("AIFORGE_REVIEWER_ITERATE", "0") in {"1", "true", "True"}


def needs_revision(review: dict[str, Any], *, min_score: float = 0.7) -> bool:
    """True when the review warrants another doer pass.

    Returns True if the review is empty, the verdict is
    ``request_changes``, any *critical* axis (correctness/security/
    regression) falls below ``min_score`` (normalized 0-1), or the overall
    axis average falls below ``min_score``.
    """
    if not review:
        return True
    if review.get("verdict") == "request_changes":
        return True
    axes = review.get("axes") or {}
    scores = [
        axes[ax] / _AXIS_MAX
        for ax in _ALL_AXES
        if isinstance(axes.get(ax), (int, float))
    ]
    if not scores:
        return True
    # any critical axis below threshold
    for ax in _CRITICAL_AXES:
        val = axes.get(ax)
        if isinstance(val, (int, float)) and val / _AXIS_MAX < min_score:
            return True
    # overall average below threshold
    return (sum(scores) / len(scores)) < min_score


def extract_fix_list(review: dict[str, Any]) -> list[str]:
    """Turn low-scoring axes + their rationales into actionable bullets.

    Each bullet names the failing axis and, when present, its per-axis
    rationale (``{axis}_rationale``) or the overall ``rationale``. Clean
    axes (score == max) are omitted. Returns ``[]`` when nothing is wrong.
    """
    axes = (review or {}).get("axes") or {}
    overall = (review or {}).get("rationale", "")
    fixes: list[str] = []
    for ax in _ALL_AXES:
        val = axes.get(ax)
        if not isinstance(val, (int, float)):
            continue
        if val >= _AXIS_MAX:
            continue
        detail = review.get(f"{ax}_rationale") or overall or ""
        bullet = f"[{ax}] score {val}/{int(_AXIS_MAX)}"
        if detail:
            bullet += f": {detail}"
        fixes.append(bullet)
    return fixes


def review_rounds(
    run_review,
    apply_fixes,
    *,
    max_rounds: int = 2,
    min_score: float = 0.7,
) -> dict[str, Any]:
    """Loop orchestrator for the reviewer<->doer handshake (pure).

    ``run_review() -> dict`` produces a review result; ``apply_fixes(
    fix_list: list[str]) -> None`` hands the doer the actionable bullets.
    Each round: run a review; if it does not need revision, return
    ``{"rounds": n, "status": "approved", "review": <review>}``; otherwise
    apply the extracted fix list and loop. After ``max_rounds`` failing
    reviews, return ``{"rounds": n, "status": "max_rounds", ...}``.
    """
    last_review: dict[str, Any] = {}
    for n in range(1, max_rounds + 1):
        last_review = run_review() or {}
        if not needs_revision(last_review, min_score=min_score):
            return {"rounds": n, "status": "approved", "review": last_review}
        apply_fixes(extract_fix_list(last_review))
    return {"rounds": max_rounds, "status": "max_rounds", "review": last_review}


def review_pr(
    pr_url: str,
    ticket_title: str,
    ticket_body: str,
) -> dict[str, Any]:
    """Run the Reviewer pass on a freshly-opened PR. Returns
    ``{ok, verdict, rationale, posted}``.

    Disable with ``AIFORGE_PR_REVIEW=0``.
    """
    if os.environ.get("AIFORGE_PR_REVIEW", "1") in {"0", "false", ""}:
        return {"ok": False, "error": "disabled"}
    if shutil.which("gh") is None:
        return {"ok": False, "error": "missing_gh"}
    parsed = _parse_pr_url(pr_url)
    if parsed is None:
        return {"ok": False, "error": "bad_pr_url"}
    owner, repo, num = parsed
    diff = _gh_pr_diff(owner, repo, num)
    if not diff.strip():
        return {"ok": False, "error": "empty_diff"}
    prompt = (
        _REVIEW_PROMPT
        .replace("{{title}}", (ticket_title or "")[:200])
        .replace("{{body}}", (ticket_body or "")[:2000])
        .replace("{{diff}}", diff)
    )
    verdict_obj = _llm_review(prompt)
    if not verdict_obj:
        return {"ok": False, "error": "llm_no_verdict"}
    verdict = verdict_obj.get("verdict", "comment")
    rationale = verdict_obj.get("rationale", "")
    axes = {
        ax: verdict_obj.get(ax)
        for ax in ("scope", "correctness", "security", "regression", "style")
    }
    body = (
        f"**aiforge:reviewer** verdict=`{verdict}`\n\n"
        f"{rationale}\n\n"
        f"| axis | score |\n|---|---|\n"
        + "\n".join(f"| {k} | {v} |" for k, v in axes.items())
    )
    posted = _gh_pr_comment(owner, repo, num, body)
    return {"ok": True, "verdict": verdict,
            "rationale": rationale, "axes": axes,
            "posted": posted}


__all__ = [
    "review_pr",
    "_extract_review_json",
    "needs_revision",
    "extract_fix_list",
    "review_rounds",
    "iterate_enabled",
]
