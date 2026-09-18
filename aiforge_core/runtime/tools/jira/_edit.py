"""Jira tools that change an issue: create, update, comments, transitions, assignment and links."""
from __future__ import annotations

import re
import urllib.parse

from ..edit_merge import EditError, apply_edit


def _pkg():
    """``jira``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``jira``; patch any other
    name on this module."""
    import aiforge_core.runtime.tools.jira as package
    return package


def jira_create(args: dict, _cwd: str | None = None) -> dict:
    """Create an issue. Required: ``project`` (key), ``summary``. Optional:
    ``issuetype`` (name, default 'Task'), ``description``, ``priority`` (name),
    ``labels`` (list), ``assignee`` (name), ``parent`` (key, for sub-tasks)."""
    pkg = _pkg()
    if not args.get("project") and pkg.default_project():
        args = {**args, "project": pkg.default_project()}
    for k in ("project", "summary"):
        if not args.get(k):
            return {"ok": False, "error": f"missing '{k}'"}
    fields: dict = {
        "project": {"key": args["project"]},
        "summary": args["summary"],
        "issuetype": {"name": args.get("issuetype") or "Task"},
    }
    if args.get("description"):
        fields["description"] = pkg.to_jira_wiki(str(args["description"]))
    if args.get("priority"):
        fields["priority"] = {"name": args["priority"]}
    if args.get("assignee"):
        fields["assignee"] = {"name": args["assignee"]}
    if args.get("labels"):
        labels = args["labels"]
        if isinstance(labels, str):
            labels = [s.strip() for s in labels.split(",") if s.strip()]
        fields["labels"] = labels
    if args.get("parent"):
        fields["parent"] = {"key": str(args["parent"])}
    r = pkg._request("POST", "/rest/api/2/issue", body={"fields": fields})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    key = d.get("key", "")
    return {"ok": True, "key": key, "url": pkg._issue_url(key),
            "written": {"summary": args.get("summary"),
                        "description": args.get("description")}}


def _wanted_status(args: dict, raw_fields: dict) -> str:
    """Jira status is NOT an editable field — it changes only via a workflow
    transition. A ``status``/``state`` arg (or a ``status`` inside ``fields``)
    is routed to jira_transition, so "move CLR-1 to In Progress" works whether
    the agent calls jira_update or jira_transition."""
    want = (args.get("status") or args.get("state") or "").strip()
    if want or raw_fields.get("status") is None:
        return want
    st = raw_fields.pop("status")
    return (st.get("name") if isinstance(st, dict) else str(st)).strip()


def description_args(args: dict) -> dict:
    """``args`` with a raw ``fields.description`` folded into ``description``
    (the named arg wins) and removed from ``fields`` — so the raw path gets the
    same merge + guard, and can never overwrite a merged description after it.
    A raw ``None`` means "clear it" (as Jira reads it): ``""``."""
    raw = args.get("fields")
    if not isinstance(raw, dict) or "description" not in raw:
        return args
    raw = dict(raw)
    val = raw.pop("description")
    out = {**args, "fields": raw}
    if args.get("description") is None:
        out["description"] = "" if val is None else val
    return out


def merged_description(current: str, args: dict) -> str:
    """The description to write for ``args`` against the ``current`` one —
    the SAME merge jira_update performs, so the approval preview shows exactly
    what will be written (see edit_merge: mode/section/find/allow_loss).
    Raises EditError."""
    from ..edit_merge import _mask
    crlf = lambda t: (t or "").replace("\r\n", "\n")  # noqa: E731
    current = crlf(current)          # Jira Server stores browser edits as CRLF
    args = {**args, "find": crlf(args.get("find")) or None}
    # An edit of an issue that numbers its steps "# step" sends "# next step";
    # that is one more list item, not an H1. (Comments in {code} don't count.)
    as_list = bool(re.search(r"^[ \t]*#[ \t]+\S", _mask(current, "wiki"), re.M))
    to_wiki = _pkg().to_jira_wiki
    text = crlf(str(args["description"]))
    fragment = (to_wiki(text, hash_is_list=True) if as_list else to_wiki(text)) or ""
    if (args.get("mode") or "").strip().lower() == "replace_text" and fragment:
        # The converter tidies (strips) its output; a text swap must keep the
        # line breaks around it or two lines are glued together.
        lead = text[:len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        fragment = lead + fragment.strip() + trail
    return apply_edit(current, fragment, args, kind="wiki")


def _current_description(key: str) -> "str | dict":
    """The issue's description now, or the error dict of the read."""
    pkg = _pkg()
    r = pkg._request("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}",
                     params={"fields": "description"})
    if not r["ok"]:
        return r
    if not isinstance(r["data"], dict):
        # Over the response cap (or not JSON): merging into "" would write the
        # fragment alone over the whole description.
        return {"ok": False, "error": "could not read the current description "
                "(response too large or unreadable) — not editing it"}
    return str(((r["data"].get("fields") or {}) or {}).get("description") or "")


def _update_fields(args: dict, raw_fields: dict,
                   description: "str | None" = None) -> dict:
    fields: dict = {}
    if args.get("summary"):
        fields["summary"] = args["summary"]
    if description is not None:
        fields["description"] = description
    if args.get("priority"):
        fields["priority"] = {"name": args["priority"]}
    if args.get("assignee"):
        fields["assignee"] = {"name": args["assignee"]}
    if args.get("labels") is not None:
        labels = args["labels"]
        if isinstance(labels, str):
            labels = [s.strip() for s in labels.split(",") if s.strip()]
        fields["labels"] = labels
    if raw_fields:                       # status already popped out
        fields.update(raw_fields)
    return fields


def jira_update(args: dict, cwd: str | None = None) -> dict:
    """Update issue fields. Required: ``key``. Provide any of ``summary``,
    ``description``, ``priority`` (name), ``labels`` (list), ``assignee``
    (name), ``status`` (auto-routed to a workflow transition), or a raw
    ``fields`` dict (merged last, wins)."""
    pkg = _pkg()
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": pkg._MISSING_KEY}
    args = description_args(args)
    raw_fields = dict(args["fields"]) if isinstance(args.get("fields"), dict) else {}
    status_want = _wanted_status(args, raw_fields)
    # The description is read, merged and guarded BEFORE any transition: a
    # refused edit must not leave the status already moved.
    description = None
    if args.get("description") is not None:
        current = _current_description(key)
        if isinstance(current, dict):
            return current
        try:
            description = merged_description(current, args)
        except EditError as exc:
            return {"ok": False, "error": str(exc),
                    "description_chars": len(current)}
    transitioned = None
    if status_want:
        tr = pkg.jira_transition({"key": key, "transition": status_want,
                              "comment": args.get("comment")}, cwd)
        if not tr.get("ok"):
            return tr
        transitioned = status_want
    fields = _update_fields(args, raw_fields, description)
    if not fields:
        # A status-only change is legit (it went through the transition above).
        if transitioned:
            return {"ok": True, "key": key, "status": transitioned,
                    "transitioned": True, "url": pkg._issue_url(key)}
        return {"ok": False, "error": "no fields to update"}
    r = pkg._request("PUT", f"/rest/api/2/issue/{urllib.parse.quote(key)}",
                 body={"fields": fields})
    if not r["ok"]:
        return r
    written = {k: args[k] for k in ("summary", "description", "priority",
               "labels", "assignee") if args.get(k) is not None}
    if transitioned:
        written["status"] = transitioned
    return {"ok": True, "key": key, "url": pkg._issue_url(key),
            "transitioned": bool(transitioned), "written": written}


def jira_comments(args: dict, _cwd: str | None = None) -> dict:
    """READ every comment on an issue by ``key`` — author, body, timestamps.

    Exists because the only comment tool used to be the WRITE one
    (``jira_comment``), so "show me the comments on ENG-123" had no matching
    read tool and the model reached for the poster instead. Paginated newest
    call-order; ``limit`` caps the page (default 50).
    """
    pkg = _pkg()
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": pkg._MISSING_KEY}
    try:
        limit = max(1, min(100, int(args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    r = pkg._request("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}/comment",
                 params={"maxResults": limit})
    if not r["ok"]:
        return r
    data = r["data"] if isinstance(r["data"], dict) else {}
    rows = []
    for c in (data.get("comments") or []):
        rows.append({
            "id": c.get("id"),
            "author": ((c.get("author") or {}) or {}).get("displayName"),
            "created": c.get("created"),
            "updated": c.get("updated"),
            "body": (c.get("body") or "")[:4000],
        })
    total = data.get("total")
    return {"ok": True, "key": key, "comments": rows, "count": len(rows),
            "total": total,
            "truncated": isinstance(total, int) and total > len(rows),
            "url": pkg._issue_url(key)}


def jira_comment(args: dict, _cwd: str | None = None) -> dict:
    """WRITE a NEW comment onto an issue. Required: ``key``, ``body``.

    To READ existing comments use ``jira_comments`` (plural) — this posts.
    """
    pkg = _pkg()
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": pkg._MISSING_KEY}
    if not args.get("body"):
        return {"ok": False, "error": "missing 'body'"}
    # Server/DC v2 renders WIKI markup — convert the agent's HTML/Markdown body
    # so it doesn't post with literal <p>/<strong>/## tags.
    r = pkg._request("POST", f"/rest/api/2/issue/{urllib.parse.quote(key)}/comment",
                 body={"body": pkg.to_jira_wiki(str(args["body"]))})
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    return {"ok": True, "key": key, "id": d.get("id"), "url": pkg._issue_url(key),
            "written": {"comment": str(args["body"])[:2000]}}


def jira_transitions(args: dict, _cwd: str | None = None) -> dict:
    """List the workflow transitions currently available for an issue
    (id + name + target status). Required: ``key``."""
    pkg = _pkg()
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": pkg._MISSING_KEY}
    r = pkg._request("GET", f"/rest/api/2/issue/{urllib.parse.quote(key)}/transitions")
    if not r["ok"]:
        return r
    d = r["data"] if isinstance(r["data"], dict) else {}
    trs = [{"id": t.get("id"), "name": t.get("name"),
            "to": (t.get("to") or {}).get("name")}
           for t in (d.get("transitions") or [])]
    return {"ok": True, "key": key, "transitions": trs}


def jira_transition(args: dict, _cwd: str | None = None) -> dict:
    """Move an issue through its workflow (e.g. To Do → In Progress → Done).
    Required: ``key`` + ``transition`` (a transition id, its name, or the target
    status name — matched case-insensitively). Optional ``comment``."""
    pkg = _pkg()
    key = (args.get("key") or args.get("id") or "").strip()
    if not key:
        return {"ok": False, "error": pkg._MISSING_KEY}
    want = str(args.get("transition") or args.get("to")
               or args.get("status") or args.get("name") or "").strip()
    if not want:
        return {"ok": False, "error": "missing 'transition' (name, id or status)"}
    lst = pkg.jira_transitions({"key": key})
    if not lst["ok"]:
        return lst
    tid = None
    for t in lst["transitions"]:
        if (str(t.get("id")) == want
                or (t.get("name") or "").lower() == want.lower()
                or (t.get("to") or "").lower() == want.lower()):
            tid = t.get("id")
            break
    if tid is None:
        return {"ok": False, "error": f"no transition matching '{want}'",
                "available": [t.get("name") for t in lst["transitions"]]}
    body: dict = {"transition": {"id": tid}}
    if args.get("comment"):
        body["update"] = {"comment": [{"add": {"body": args["comment"]}}]}
    r = pkg._request("POST",
                 f"/rest/api/2/issue/{urllib.parse.quote(key)}/transitions",
                 body=body)
    if not r["ok"]:
        return r
    return {"ok": True, "key": key, "transitioned_to": want,
            "url": pkg._issue_url(key)}


def jira_assign(args: dict, _cwd: str | None = None) -> dict:
    """Assign an issue to a user. Required: ``key``, ``assignee`` (username;
    ``"-1"`` / ``"unassigned"`` clears the assignee)."""
    pkg = _pkg()
    key = (args.get("key") or args.get("id") or "").strip()
    who = str(args.get("assignee") or args.get("user") or "").strip()
    if not key:
        return {"ok": False, "error": pkg._MISSING_KEY}
    if not who:
        return {"ok": False, "error": "missing 'assignee'"}
    name = None if who.lower() in ("-1", "unassigned", "none", "") else who
    r = pkg._request("PUT", f"/rest/api/2/issue/{urllib.parse.quote(key)}/assignee",
                 body={"name": name})
    if not r["ok"]:
        return r
    return {"ok": True, "key": key, "assignee": name or "(unassigned)",
            "url": pkg._issue_url(key)}


def jira_link_issues(args: dict, _cwd: str | None = None) -> dict:
    """Link two issues. Required: ``inward`` + ``outward`` (issue keys) and
    ``type`` (link-type name, e.g. 'Blocks', 'Relates', 'Duplicate'). Semantics:
    inward <type> outward (e.g. inward BLOCKS outward)."""
    inward = str(args.get("inward") or args.get("from") or "").strip()
    outward = str(args.get("outward") or args.get("to") or "").strip()
    ltype = str(args.get("type") or args.get("link_type") or "Relates").strip()
    if not inward or not outward:
        return {"ok": False, "error": "need 'inward' + 'outward' issue keys"}
    body: dict = {"type": {"name": ltype},
                  "inwardIssue": {"key": inward},
                  "outwardIssue": {"key": outward}}
    if args.get("comment"):
        body["comment"] = {"body": str(args["comment"])}
    r = _pkg()._request("POST", "/rest/api/2/issueLink", body=body)
    if not r["ok"]:
        return r
    return {"ok": True, "linked": {"inward": inward, "outward": outward,
                                   "type": ltype}}
