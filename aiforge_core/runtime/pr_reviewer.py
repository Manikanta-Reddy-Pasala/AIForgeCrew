"""Second-agent PR review (standards gap C2).

After ``commit_push_open_pr`` succeeds, run a Reviewer pass over the
final diff and post the verdict as a PR comment. KISS: no new ADK
LlmAgent (the Refiner already does in-loop review). One blocking
LiteLLM call against the planner model is enough.
"""
from __future__ import annotations

import json
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
    """Single LiteLLM call. Returns parsed JSON or ``{}`` on failure."""
    try:
        import litellm
    except ImportError:
        return {}
    model = os.environ.get(
        "AIFORGE_REVIEWER_MODEL", "openai/qwen3-coder-next",
    )
    base = os.environ.get("AIFORGE_LM_BASE_URL", "http://127.0.0.1:1234/v1")
    api_key = os.environ.get("AIFORGE_LM_API_KEY", "lm-studio")
    try:
        resp = litellm.completion(
            model=model,
            api_base=base, api_key=api_key,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1, timeout=120,
        )
        text = resp["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        log.warning("pr_reviewer LLM failed: %s", exc)
        return {}
    # Extract first JSON object from possibly-markdown response.
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


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


__all__ = ["review_pr"]
