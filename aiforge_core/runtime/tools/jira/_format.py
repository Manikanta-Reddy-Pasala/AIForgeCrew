"""Pure issue/time formatting helpers — seconds→'Xh Ym', the time-tracking view
and the compact search-hit summary. Dependency-free (stdlib only); shared by the
issue tools and the agile/project tools.

Split out of the former ``jira.py`` module; behaviour is unchanged.
"""
from __future__ import annotations


def _fmt_secs(secs) -> str | None:
    """Jira-style 'Xh Ym' from a seconds count (None-safe)."""
    try:
        s = int(secs)
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return "0m"
    # Jira convention: 1w=5d, 1d=8h.
    parts, units = [], (("w", 5 * 8 * 3600), ("d", 8 * 3600),
                        ("h", 3600), ("m", 60))
    for label, size in units:
        if s >= size:
            parts.append(f"{s // size}{label}")
            s %= size
    return " ".join(parts) or "0m"


def _time_fields(f: dict) -> dict:
    """Time-tracking view of an issue: original estimate, remaining, and time
    spent — both human ('2h 30m') and raw seconds. Reads the ``timetracking``
    object first (has the pretty strings), then the flat second-fields as a
    fallback. ``aggregate*`` include sub-tasks."""
    tt = f.get("timetracking") if isinstance(f.get("timetracking"), dict) else {}
    orig_s = tt.get("originalEstimateSeconds")
    if orig_s is None:
        orig_s = f.get("timeoriginalestimate")
    rem_s = tt.get("remainingEstimateSeconds")
    if rem_s is None:
        rem_s = f.get("timeestimate")
    spent_s = tt.get("timeSpentSeconds")
    if spent_s is None:
        spent_s = f.get("timespent")
    agg_spent = f.get("aggregatetimespent")
    return {
        "original_estimate": tt.get("originalEstimate") or _fmt_secs(orig_s),
        "remaining_estimate": tt.get("remainingEstimate") or _fmt_secs(rem_s),
        "time_spent": tt.get("timeSpent") or _fmt_secs(spent_s),
        "original_estimate_seconds": orig_s,
        "remaining_estimate_seconds": rem_s,
        "time_spent_seconds": spent_s,
        "aggregate_time_spent": _fmt_secs(agg_spent),
        "aggregate_time_spent_seconds": agg_spent,
    }


# Fields requested for the time-tracking view — reused by search + read.
_TIME_FIELDS = ("timetracking,timespent,timeoriginalestimate,timeestimate,"
                "aggregatetimespent")


def _issue_summary(d: dict, *, with_time: bool = False) -> dict:
    """Compact one-line view of an issue search hit."""
    f = d.get("fields") if isinstance(d.get("fields"), dict) else {}
    out = {
        "key": d.get("key"),
        "summary": f.get("summary"),
        "type": ((f.get("issuetype") or {}) or {}).get("name"),
        "status": ((f.get("status") or {}) or {}).get("name"),
        "assignee": ((f.get("assignee") or {}) or {}).get("displayName"),
    }
    if with_time:
        out["time"] = _time_fields(f)
    return out
