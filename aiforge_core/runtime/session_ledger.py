"""Session execution ledger — what the agent ALREADY did this session.

Every turn's tool calls are stored per message (``chat_messages.steps``), but
nothing fed a clean "already executed" view back into the next turn, so on a
follow-up the model re-derived from prose history and RE-RAN steps it had
already done. This module distils the session's prior tool calls into a compact,
deduped ledger — the exact command that worked, the files written, the external
writes — and :func:`ledger_block` renders it for injection into the turn context
("do NOT repeat unless it failed / the user asks").

Read-only + soft-fail: a ledger error must never break a turn.
"""
from __future__ import annotations

import os

# Tools whose args carry a shell command.
_CMD_TOOLS = {"run_command", "bash", "run_shell", "shell", "serve"}
# File-mutating tools → key by path.
_WRITE_TOOLS = {"file_write", "file_create", "editor"}
_PATCH_TOOLS = {"file_patch"}
# External writes worth remembering by a stable identity.
_EXTERNAL = {
    "confluence_create": ("confluence page", ("title",)),
    "confluence_update": ("confluence page", ("id", "title")),
    "jira_create": ("jira issue", ("summary",)),
    "jira_update": ("jira issue", ("key",)),
    "jira_comment": ("jira comment", ("key",)),
    "github_pr": ("github PR", ("title",)),
    "gitlab_mr_create": ("gitlab MR", ("title", "source_branch")),
    "note_consolidate": ("note update", ("path",)),
    "note_curate": ("note curate", ("path",)),
}


def _cap() -> int:
    try:
        return max(400, int(os.environ.get("AIFORGE_SESSION_LEDGER_CAP", "2400")))
    except (TypeError, ValueError):
        return 2400


def _ok(result) -> "bool | None":
    if isinstance(result, dict):
        v = result.get("ok")
        return v if isinstance(v, bool) else None
    return None


def _summarize(name: str, args: dict, result) -> "dict | None":
    """One tool step → a ledger entry ``{key, label, outcome}`` or None (a
    read-only / uninteresting call). ``key`` dedupes; ``outcome`` is ok/fail."""
    ok = _ok(result)
    if name in _CMD_TOOLS:
        cmd = str(args.get("cmd") or args.get("command") or "").strip()
        if not cmd:
            return None
        return {"key": f"cmd:{cmd}", "label": f"ran `{cmd[:200]}`", "outcome": ok}
    if name in _WRITE_TOOLS:
        path = str(args.get("path") or args.get("file") or "").strip()
        if not path:
            return None
        return {"key": f"write:{path}", "label": f"wrote `{path}`", "outcome": ok}
    if name in _PATCH_TOOLS:
        path = str(args.get("path") or "").strip()
        if not path:
            return None
        return {"key": f"patch:{path}", "label": f"patched `{path}`", "outcome": ok}
    if name in _EXTERNAL:
        kind, id_fields = _EXTERNAL[name]
        ident = next((str(args[f]) for f in id_fields if args.get(f)), "")
        return {"key": f"{name}:{ident}",
                "label": f"{kind}: {ident[:120]}" if ident else kind,
                "outcome": ok}
    return None


def ledger_items(session_id) -> list[dict]:
    """Deduped, in-order list of executed actions for the session. Later calls
    to the SAME action update its outcome (a retry that finally succeeded shows
    as ok). Never raises."""
    try:
        from aiforge_core.runtime import chat_store
        msgs = chat_store.get_messages(session_id) or []
    except Exception:  # noqa: BLE001
        return []
    seen: dict[str, dict] = {}
    order: list[str] = []
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for s in (m.get("steps") or []):
            if not isinstance(s, dict) or s.get("type") != "tool":
                continue
            entry = _summarize(s.get("name") or "", s.get("args") or {},
                               s.get("result"))
            if not entry:
                continue
            k = entry["key"]
            if k in seen:
                seen[k]["outcome"] = entry["outcome"]   # latest outcome wins
            else:
                seen[k] = entry
                order.append(k)
    return [seen[k] for k in order]


def ledger_block(session_id) -> str:
    """The context block. Empty string when nothing has been executed yet."""
    items = ledger_items(session_id)
    if not items:
        return ""
    lines = []
    for it in items:
        mark = "✅" if it["outcome"] is True else "❌" if it["outcome"] is False else "•"
        lines.append(f"- {mark} {it['label']}")
    body = "\n".join(lines)
    cap = _cap()
    if len(body) > cap:                       # keep the MOST RECENT within budget
        kept, size = [], 0
        for ln in reversed(lines):
            if size + len(ln) + 1 > cap:
                break
            kept.append(ln)
            size += len(ln) + 1
        body = "\n".join(reversed(kept))
    return ("ALREADY EXECUTED THIS SESSION — do NOT repeat any of these unless it "
            "FAILED (❌) or the user explicitly asks to redo it. When you need one "
            "again, reuse the EXACT command shown:\n" + body)


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", (s or "").strip().lower()).strip("-")[:48] \
        or "session"


def _min_steps() -> int:
    try:
        return max(1, int(os.environ.get("AIFORGE_SESSION_WORKFLOW_MIN", "2")))
    except (TypeError, ValueError):
        return 2


_VERIFY_SYS = (
    "You verify whether a chat session's SUCCESSFUL shell commands form a "
    "REUSABLE workflow worth saving as a playbook. Given the ordered commands, "
    "decide `is_reusable`: true ONLY if they form a coherent, repeatable "
    "procedure (e.g. build→test→deploy), false for random one-off exploration. "
    "If reusable: return a short kebab-case `name`, a one-line `description`, "
    "the cleaned ordered `steps` (DROP noise / duplicate / exploratory commands, "
    "keep only the essential procedure; fix obvious ordering), and 1-3 short "
    "`triggers`. If not coherent, set is_reusable=false and leave the rest empty. "
    "Do NOT invent commands not in the input."
)


def _verify_workflow(title: str, commands: list[str]) -> "dict | None":
    """LLM verification of a candidate workflow. Returns a dict
    ``{is_reusable, name, description, steps, triggers}`` or None when no model
    is reachable (caller then saves the raw capture). Never raises."""
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete

        class _Verified(BaseModel):
            is_reusable: bool = False
            name: str = ""
            description: str = ""
            steps: list[str] = []
            triggers: list[str] = []

        payload = {"session_title": title, "successful_commands": commands}
        import json as _json
        res = structured_complete(
            "learner",
            [{"role": "system", "content": _VERIFY_SYS},
             {"role": "user", "content": _json.dumps(payload, ensure_ascii=False)}],
            _Verified, max_retries=1, max_tokens=1200, temperature=0.1)
        return res.model_dump()
    except Exception:  # noqa: BLE001 — no model / bad JSON → caller falls back
        return None


def capture_working_workflow(session_id, repo: str = "repo") -> dict:
    """Auto-author a reusable WORKFLOW from the session's WORKING steps (the
    commands that succeeded, in order) and file it into OKR knowledge memory
    with proper tags. Skips a session with too few working steps. Soft-fail.

    write_workflow already mirrors the workflow into knowledge memory
    (kind=workflow); we ALSO capture a topic-tagged OKR note so the working
    procedure lands in the topic briefs. Disable with AIFORGE_SESSION_WORKFLOW=0.
    """
    if os.environ.get("AIFORGE_SESSION_WORKFLOW", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return {"ok": False, "skipped": "disabled"}
    items = ledger_items(session_id)
    cmds = [i for i in items if i["outcome"] is True and i["key"].startswith("cmd:")]
    writes = [i for i in items
              if i["outcome"] is True and i["key"].startswith(("write:", "patch:"))]
    if len(cmds) < _min_steps():
        return {"ok": True, "skipped": "too_few_working_steps"}
    try:
        from aiforge_core.runtime import chat_store
        title = (chat_store.get_session(session_id) or {}).get("title") \
            or f"session {session_id}"
    except Exception:  # noqa: BLE001
        title = f"session {session_id}"
    slug = _slug(title)
    raw_cmds = [c["key"][4:] for c in cmds]
    # VERIFY before creating: an LLM checks the successful commands actually form
    # a coherent, reusable procedure (not random one-offs), and refines the
    # name/description/ordered steps. A non-reusable session is skipped, so we
    # don't save junk workflows. Falls back to the raw capture if no model.
    verified = _verify_workflow(title, raw_cmds)
    if verified is not None and not verified.get("is_reusable", False):
        return {"ok": True, "skipped": "not_reusable"}
    if verified:
        name = f"session-{_slug(verified.get('name') or title)}"
        description = (verified.get("description") or f"Working steps: {title}")[:120]
        steps = [str(s) for s in (verified.get("steps") or raw_cmds) if str(s).strip()]
        triggers = [_slug(t) for t in (verified.get("triggers") or [slug]) if t][:4] or [slug]
        verified_note = ""
    else:                                   # no model — save the raw capture
        name = f"session-{slug}"
        description = f"Captured working steps: {title}"[:120]
        steps = raw_cmds
        triggers = [slug]
        verified_note = "\n\n_(unverified — no model reachable at capture time)_"
    steps_md = "\n".join(f"{n + 1}. `{s}`" for n, s in enumerate(steps))
    files_md = ("\n\nFiles touched:\n"
                + "\n".join(f"- {w['label']}" for w in writes)) if writes else ""
    body = (f"Reusable procedure verified from chat session {session_id} "
            f"({title}). Run these in order for the same task instead of "
            f"re-deriving:\n\n{steps_md}{files_md}{verified_note}")
    tags = ["session", "workflow", f"session:{session_id}"]
    if repo and repo != "repo":
        tags.append(f"repo:{repo}")
    out = {"ok": False}
    try:
        from aiforge_core.runtime import workflows
        # write_workflow itself hard-runs/syntax-checks any scripts before save.
        out = workflows.write_workflow(
            name, description=description, body=body, triggers=triggers,
            scope="global")
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "error": str(exc)}
    # OKR topic note (proper tags) so the working procedure is in the briefs too.
    try:
        from aiforge_core.memory import md_store
        summary = "; ".join(c["key"][4:][:60] for c in cmds[:6])
        md_store.capture(
            "workflow", f"{title}: {len(cmds)} working steps — {summary}",
            repo=(repo if repo != "repo" else "notes"), topic=slug,
            title=f"Working steps — {title}", tags=tags, source=f"session:{session_id}")
    except Exception:  # noqa: BLE001
        pass
    return out


__all__ = ["ledger_items", "ledger_block", "capture_working_workflow"]
