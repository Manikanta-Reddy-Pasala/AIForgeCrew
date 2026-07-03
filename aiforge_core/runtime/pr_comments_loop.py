"""PR-comment response loop (standards gap C7).

KISS systemd-friendly entrypoint: scan ``gh pr list`` for open PRs
authored by us that have new comments since last run, and create a
follow-up ticket per fresh comment so the Doer pipeline picks them
up. Run from a timer (one-shot).

State (last-seen comment id per PR) lives at
``$HOME/.aiforge/pr_comments_seen.json`` so the script is idempotent.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("aiforge.pr_comments_loop")


_STATE_PATH = Path(os.environ.get(
    "AIFORGE_PR_COMMENTS_STATE",
    # Under the shared config dir (not raw home) so the "seen comment ids" state
    # persists on the mounted volume — else it's lost on container restart and
    # already-handled PR comments re-emit duplicate tickets.
    os.path.join(
        os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")),
        "pr_comments_seen.json"),
))


def _load_state() -> dict:
    if not _STATE_PATH.is_file():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state, indent=2),
                               encoding="utf-8")
    except OSError as exc:
        log.warning("state save failed: %s", exc)


def _gh_json(args: list[str]) -> list[dict] | dict | None:
    try:
        proc = subprocess.run(
            ["gh"] + args, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _api_url(owner: str, repo: str, pr: int, *, latest_id: int | None) -> str:
    base = f"/repos/{owner}/{repo}/issues/{pr}/comments"
    return base if latest_id is None else f"{base}?since=2000-01-01"


_QUESTION_STARTERS = (
    "who", "what", "why", "how", "where",
    "can", "could", "should", "is", "are", "does",
)
_NIT_KEYWORDS = ("nit", "typo", "style", "lint", "rename", "format")


def classify_comment(body: str) -> str:
    """Pure heuristic classifier.

    Returns one of ``"question"`` | ``"nit"`` | ``"change_request"``.

    - ends with ``?`` or starts with a question word -> ``"question"``
    - contains a nit keyword (nit/typo/style/lint/rename/format) -> ``"nit"``
    - otherwise -> ``"change_request"``
    """
    text = (body or "").strip()
    if not text:
        return "change_request"
    lowered = text.lower()
    first_word = lowered.split(None, 1)[0].strip(".,:;!?") if lowered else ""
    if text.endswith("?") or first_word in _QUESTION_STARTERS:
        return "question"
    if any(kw in lowered for kw in _NIT_KEYWORDS):
        return "nit"
    return "change_request"


def _lightweight_enabled() -> bool:
    return os.environ.get("AIFORGE_PR_COMMENT_LIGHTWEIGHT", "1") != "0"


def route_comment(comment: dict) -> dict:
    """Route a comment to a ``lightweight`` or ``full`` handling path.

    Questions + nits go lightweight; change-requests go full. When the
    ``AIFORGE_PR_COMMENT_LIGHTWEIGHT`` env flag is ``"0"`` everything is
    forced to ``full`` (preserves the original always-ticket behavior).
    """
    cid = comment.get("id")
    kind = classify_comment(comment.get("body", ""))
    if not _lightweight_enabled():
        return {
            "mode": "full",
            "reason": f"lightweight_disabled:{kind}",
            "comment_id": cid,
        }
    if kind in ("question", "nit"):
        return {"mode": "lightweight", "reason": kind, "comment_id": cid}
    return {"mode": "full", "reason": kind, "comment_id": cid}


def lightweight_reply(comment: dict) -> dict:
    """Record the *intent* of a lightweight reply (stub).

    Does NOT post anything to GitHub yet; it just composes the planned
    reply text and returns it for logging/emission.
    """
    cid = comment.get("id")
    kind = classify_comment(comment.get("body", ""))
    snippet = (comment.get("body", "") or "").strip().splitlines()
    snippet = snippet[0][:200] if snippet else ""
    if kind == "question":
        reply_text = (
            "Thanks for the question; this looks like a clarification "
            f"rather than a code change. Re: \"{snippet}\""
        )
    else:  # nit
        reply_text = (
            "Acknowledged as a nit/style note; noting for a minor "
            f"follow-up. Re: \"{snippet}\""
        )
    return {
        "comment_id": cid,
        "kind": kind,
        "reply_text": reply_text,
        "posted": False,
    }


def _post_followup_ticket(
    *, project: str, pr_num: int, comment: dict,
) -> bool:
    api = os.environ.get("AIFORGE_API_BASE", "http://localhost:8799")
    body = (
        f"Follow-up from PR #{pr_num} comment by "
        f"{comment.get('user', {}).get('login', '?')}:\n\n"
        f"{comment.get('body', '')[:4000]}\n\n"
        f"Comment URL: {comment.get('html_url', '')}"
    )
    payload = {
        "title": f"PR #{pr_num}: address review comment",
        "body": body,
        "project": project,
        "priority": "medium",
        "labels": ["pr-followup"],
        "metadata": {
            "pr_followup": True,
            "pr_number": pr_num,
            "comment_id": comment.get("id"),
        },
    }
    try:
        import urllib.request

        from aiforge_core.net.ssl import context_for as _ssl_context_for
        url = f"{api}/api/tickets"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(
            req, timeout=10, context=_ssl_context_for(url)
        ) as resp:
            return resp.status == 201
    except Exception as exc:  # noqa: BLE001
        log.warning("ticket POST failed: %s", exc)
        return False


def run() -> dict:
    """One-shot: scan open PRs we authored, ticket each new comment."""
    if shutil.which("gh") is None:
        return {"ok": False, "error": "missing_gh"}
    state = _load_state()
    prs = _gh_json([
        "pr", "list", "--author", "@me", "--state", "open",
        "--json", "number,headRepository,headRefName,headRepositoryOwner,baseRefName",
        "--limit", "30",
    ]) or []
    new_tickets = 0
    lightweight_replies = 0
    seen = state
    for pr in prs:
        repo = pr.get("headRepository", {}).get("name") or ""
        owner = pr.get("headRepositoryOwner", {}).get("login") or ""
        if not (repo and owner):
            continue
        pr_num = pr.get("number")
        comments = _gh_json([
            "api", f"repos/{owner}/{repo}/issues/{pr_num}/comments",
            "--paginate",
        ])
        if not isinstance(comments, list):
            continue
        key = f"{owner}/{repo}#{pr_num}"
        last_id = seen.get(key, 0)
        for c in comments:
            cid = c.get("id", 0)
            if cid <= last_id:
                continue
            route = route_comment(c)
            log.info(
                "PR %s comment %s -> %s (%s)",
                key, cid, route["mode"], route["reason"],
            )
            if route["mode"] == "lightweight":
                reply = lightweight_reply(c)
                log.info(
                    "lightweight reply (not posted) for comment %s: %s",
                    cid, reply["reply_text"],
                )
                lightweight_replies += 1
                seen[key] = max(last_id, cid)
                last_id = seen[key]
                continue
            if _post_followup_ticket(project=repo, pr_num=pr_num, comment=c):
                new_tickets += 1
                seen[key] = max(last_id, cid)
                last_id = seen[key]
    _save_state(seen)
    return {"ok": True, "tickets_created": new_tickets,
            "lightweight_replies": lightweight_replies,
            "prs_scanned": len(prs)}


def main() -> int:
    out = run()
    print(json.dumps(out))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "run", "main",
    "classify_comment", "route_comment", "lightweight_reply",
]
