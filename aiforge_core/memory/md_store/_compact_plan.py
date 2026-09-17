"""Planning a compaction pass: the live captures, their topics and axes, and
the groups to fold."""
from __future__ import annotations

import re

from ._base import (
    _FM_RE,
    _capture_md_files,
    _parse,
    iter_briefs,
)
from ._compact_summarize import (
    _COMPACT_BODY_CAP,
    _NO_TOPIC,
    _group_key,
)
from ._compact_sweep import (
    _demote_headings,
)
from ._render import (
    _parse_brief,
)


def _pkg():
    """The parent module, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.memory.md_store._compact as package
    return package


def _live_capture_notes(group_by: str) -> list[dict]:
    """The raw capture units eligible for this axis.

    The REPO brief is a curated project-learning projection — raw per-session
    transcripts (kind="session") stay out of it. The TOPIC axis is exactly
    where sessions belong: memory organized BY TOPIC. Large transcripts are
    fine there now — consolidate() distils them via the LLM (chonkie chunks big
    input) into Facts/Learnings rather than dumping, and the raw file archives
    out after folding. Excluding them was why per-session memory lingered and
    compaction said "nothing to compact".
    """
    files: list[dict] = []
    for p in _capture_md_files():
        try:
            d = _parse(p)
        except Exception:  # noqa: BLE001
            continue
        d["_path"] = p
        files.append(d)
    live = [d for d in files if not d["file"].startswith("compacted-")]
    if group_by == "repo":
        live = [d for d in live if (d.get("kind") or "") != "session"]
    return live


def _label_topics(live: list[dict], model_role: str) -> None:
    """TOPIC mode: one LLM pass labels every note with a coherent topic slug so
    compaction yields several browsable topical files instead of ONE blob per
    kind. Falls back to kind grouping for any note the labeller missed (or all,
    if the model is unreachable)."""
    try:
        labels = _pkg()._topic_labels(live, model_role)
    except Exception:  # noqa: BLE001
        labels = {}
    for d in live:
        d["_topic"] = labels.get(d["file"])


def _explicit_topic(d: dict) -> str | None:
    return d.get("topic") or next(
        (t.split(":", 1)[1] for t in (d.get("tags") or [])
         if t.startswith("topic:")), None)


def _apply_topic_floor(groups: dict, live: list[dict]) -> dict:
    """A brand-new MODEL-INVENTED topic must earn its file: below the floor its
    notes stay in their repo/shared brief instead of minting a one-fact magnet
    (the `compacted-isprime-function.md` class of junk). Topics that already
    exist keep receiving regardless, and a topic the CALLER named explicitly is
    intentional — never gated."""
    from . import _topics
    floor = _topics.min_facts_for_new_topic()
    if floor <= 1:
        return groups
    keep = set(_topics.existing_topics())
    keep.update(t for t in (_explicit_topic(d) for d in live) if t)
    return {k: v for k, v in groups.items() if k in keep or len(v) >= floor}


def _brief_axis(p) -> str:
    """"repo" or "topic" — which axis owns this brief file."""
    from . import _topics
    key = p.stem[len("compacted-"):] or "shared"
    if key == "shared":
        return "repo"           # the shared brief folds with the repo axis
    try:
        # _parse_brief, not _parse: a brief's tags are a YAML BLOCK list, which
        # the line-splitting frontmatter reader returns as nothing at all.
        tags = _parse_brief(p.read_text(encoding="utf-8", errors="replace"))["tags"]
    except OSError:
        tags = []
    return "repo" if _topics.is_repo_brief(key, tags) else "topic"


def _add_existing_briefs(result: dict, group_by: str) -> None:
    """force: re-consolidate every EXISTING brief of THIS axis too — add each
    compacted-<scope>.md as its own group so the loop re-reads + re-summarises
    it even with no new live sources. Split-part / per-run-named files are
    skipped (they fold via their primary scope).

    Axis matters. Adding every brief to both axes re-folded each one TWICE per
    cycle — two lossy LLM passes and double the cost — and, worse, folding a
    repo brief on the topic axis re-indexed it as repo-agnostic, so one
    project's knowledge became visible in every other project's recall."""
    if group_by not in ("repo", "topic"):
        return
    for p in iter_briefs():
        if re.search(r"-\d{8}-[0-9a-f]{6}$", p.stem):
            continue
        if _brief_axis(p) != group_by:
            continue
        key = p.stem[len("compacted-"):] or "shared"
        result.setdefault(key, [])   # empty live → existing_body re-consolidated


def _gather_planned(group_by: str, min_group: int, model_role: str,
                    force: bool) -> dict[str, list[dict]]:
    """``{group key: notes}`` for the groups this run will fold."""
    live = _live_capture_notes(group_by)
    if group_by == "topic":
        _label_topics(live, model_role)
    groups: dict[str, list[dict]] = {}
    for d in live:
        groups.setdefault(_group_key(d, group_by), []).append(d)
    if group_by == "topic":
        # Notes the labeller couldn't theme (_NO_TOPIC) must not form a topic
        # file — they already live in their repo/shared brief.
        groups.pop(_NO_TOPIC, None)
        groups = _apply_topic_floor(groups, live)
    result = {k: v for k, v in groups.items() if len(v) >= min_group}
    if force:
        _add_existing_briefs(result, group_by)
    return result


def _prior_brief_state(path, group_by: str) -> tuple[str, list]:
    """``(existing consolidated body, prior sources)`` for a re-compaction.

    The previous file is fed back so it gets RE-SUMMARISED with the new notes,
    keeping the file bounded. For the knowledge axes (repo/topic) it is an OKR
    envelope, so only the prior consolidated PROSE is re-fed — otherwise the
    envelope text would nest inside the new body every compaction.
    """
    if not path.exists():
        return "", []
    prev = path.read_text(encoding="utf-8", errors="replace")
    prior_sources = _parse_brief(prev)["sources"]
    if group_by in ("repo", "topic"):
        return _parse_brief(prev)["body"].strip(), prior_sources
    pm = _FM_RE.match(prev)
    return (pm.group(2).strip() if pm else prev.strip()), prior_sources


def _group_blocks(items: list[dict], existing_body: str,
                  title: str) -> tuple[list[str], list[str]]:
    """``(deterministic sections, LLM blocks)`` for one group."""
    sections, blocks = [], []
    if existing_body:
        blocks.append("### (previous consolidated)\n\n" + existing_body)
    for d in items:
        meta = (f"_source: {d.get('source') or 'manual'} · "
                f"created: {d.get('created') or '?'}_")
        sections.append(f"## {d['title']}\n\n{meta}\n\n"
                        f"{_demote_headings(d['body']).strip()}".rstrip())
        blocks.append(f"### {d['title']}\n\n{d['body'].strip()}")
    return sections, blocks


def _capped_merge(body: str, title: str) -> str:
    """Bound the deterministic-merge fallback so an always-down model can't grow
    the file every run (the "file too big" problem)."""
    if len(body) <= _COMPACT_BODY_CAP:
        return body
    head = f"# {title}\n\n"
    keep = max(1000, _COMPACT_BODY_CAP - len(head) - 80)
    return (head + "_…older entries trimmed (kept in archive/); configure a "
            "model so compaction can summarise._\n\n---\n\n" + body[-keep:])
