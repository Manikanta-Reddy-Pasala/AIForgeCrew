"""Topic nodes built from the markdown briefs."""
from __future__ import annotations

from . import graph as _graph
from . import store as _store


def _pkg():
    """The package, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.memory.okf.author as package
    return package


def migrate_from_briefs() -> dict:
    """Seed the OKR graph from the existing flat briefs: each compacted-<key>.md
    (+ its split parts) → one GLOBAL Learning node (category=<topic>, body = the
    brief's Facts). Scoping to a project is NOT done here — deterministic tag/key
    parsing can't tell a repo brief from a topic brief and produces casing splits
    (AIForgeCrew vs aiforgecrew) and bogus projects (session ids). The migration
    chain's LLM ``classify`` step (which has the real repo list) sorts these
    global learnings into projects/noise afterwards, with consistent casing.
    Idempotent — a category already migrated is skipped. Soft-fail."""
    g = _graph.build(force=True)
    have = {str((n.get("meta") or {}).get("category") or "").lower()
            for n in g.nodes.values() if n.get("type") == "learning"}
    facts_by_topic = _brief_facts_by_topic()
    made = 0
    for topic, facts in facts_by_topic.items():
        if topic.lower() in have or not facts:
            continue
        body = "\n".join(f"- {f}" for f in facts)[:4000]
        r = _store.save_node("learning", None,
                             {"scope": "global", "category": topic,
                              "title": f"{topic} knowledge", "tags": [f"topic:{topic}"]},
                             body, reindex=False)
        if r.get("ok"):
            made += 1
    if made:
        _store._write_index()          # one rewrite for the whole migration
    return {"ok": True, "migrated": made, "topics": len(facts_by_topic)}


def _brief_facts_by_topic() -> dict[str, list[str]]:
    """Every locally-compacted brief's Facts, grouped by topic. Deterministic —
    no model, no network: it reads the files md_store already wrote."""
    import re

    from aiforge_core.memory import md_store
    from aiforge_core.runtime import work_notes

    out: dict[str, list[str]] = {}
    for p in md_store.iter_briefs():
        base = p.stem[len("compacted-"):]
        topic = re.sub(r"-\d+$", "", base)
        try:
            parsed = work_notes.parse_note(
                p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        if (parsed["frontmatter"] or {}).get("kind") != "knowledge":
            continue
        for raw in parsed["sections"].get("facts") or []:
            # Flattened to ONE line. A fact is written back as a single "- …"
            # bullet and read back a line at a time, so a fact containing a
            # newline would never match itself on the next pass: it would look
            # new every cycle, rewrite the node, bump `rev`, and re-trigger the
            # admin's LLM fold — forever.
            fact = " ".join(str(raw).split()).strip()
            if fact and fact not in out.setdefault(topic, []):
                out[topic].append(fact)
    return out


def _fact_lines(body: str) -> list[str]:
    """The bullet lines of a learning node's body, as plain facts.

    Whitespace-normalised the same way ``_brief_facts_by_topic`` normalises what
    it reads out of a brief, so the two halves of "is this fact already held?"
    compare in one form.
    """
    lines = []
    for raw in (body or "").splitlines():
        line = " ".join(raw.split()).strip()
        if line.startswith("- "):
            line = line[2:].strip()
        if line:
            lines.append(line)
    return lines


# How much of a topic's fact list one node carries. The cap exists because a
# node is a file an LLM later reads whole; what matters here is that it is
# applied at a LINE boundary. Cutting mid-line left a partial fact in the body,
# which then never matched the whole fact in the brief — so every cycle saw it
# as new, rewrote the node, bumped `rev`, re-pushed it, and re-triggered the
# admin's fold. Non-convergence, forever, on any topic that outgrew the cap.
_BODY_CHARS = 4000


def _body_for(facts: list[str]) -> tuple[str, list[str]]:
    """Render facts as bullets within the cap, and say which ones fitted.

    Returns ``(body, kept)``. Whole lines only — see ``_BODY_CHARS``.
    """
    kept: list[str] = []
    size = 0
    # NEWEST first-served. Filling from the front meant that once a node hit the
    # cap it froze: every fact learned afterwards fell off the end and the node
    # kept the same oldest 4000 characters forever, so the newest knowledge was
    # the one thing that never reached another machine.
    for fact in reversed(facts):
        line = f"- {fact}"
        cost = len(line) + (1 if kept else 0)
        if size + cost > _BODY_CHARS:
            break
        kept.append(fact)
        size += cost
    kept.reverse()                     # back into the order they were learned
    return "\n".join(f"- {f}" for f in kept), kept


def _learning_by_topic(g) -> dict:
    """``{topic: (id, node)}`` for the existing global learning nodes."""
    out: dict = {}
    for nid, node in g.nodes.items():
        if node.get("type") != "learning":
            continue
        cat = str((node.get("meta") or {}).get("category") or "").lower()
        if cat:
            out.setdefault(cat, (nid, node))
    return out


def _topic_scope(topic: str) -> str:
    """``global`` for a topic brief, ``repo:<name>`` for a project one.

    A repo brief published as a global node put one project's facts in front of
    every other project, on every machine it synced to — the exact scope leak
    the brief axes exist to prevent."""
    try:
        from aiforge_core.memory.md_store import _topics
        return f"repo:{topic}" if _topics.is_repo_brief(topic) else "global"
    except Exception:  # noqa: BLE001 — unknown ⇒ the narrower claim is not safe
        return "global"                                    # to invent either way


def _create_topic_node(topic: str, facts: list) -> tuple[int, int]:
    """``(created, dropped)`` for a topic no node holds yet."""
    body, kept = _body_for(facts)
    ok = _store.save_node("learning", None,
                          {"scope": _topic_scope(topic), "category": topic,
                           "title": f"{topic} knowledge",
                           "tags": [f"topic:{topic}"]},
                          body, reindex=False).get("ok")
    return (1 if ok else 0), len(facts) - len(kept)


def _update_topic_node(topic: str, facts: list, held) -> tuple[int, int]:
    """``(updated, dropped)`` for a topic that already has a node."""
    nid, node = held
    have = _fact_lines(node.get("body") or "")
    fresh = [f for f in facts if f not in have]
    if not fresh:
        return 0, 0       # unchanged: no write, no rev bump, nothing to sync
    # The BRIEF's own ordered list, not `have + fresh`: the node keeps the
    # newest facts that fit, and that choice is only stable if the input order
    # is. Concatenating what the node already held with what is new reorders
    # them on every pass, so the cap-reached guard below never matched and a
    # full topic rewrote itself (and re-triggered the admin fold) forever.
    body, kept = _body_for(facts)
    dropped = len(facts) - len(kept)
    if _fact_lines(body) == have:
        # The node is full: every fresh fact fell off the end, so writing would
        # produce byte-identical content at a higher rev — and would do so on
        # EVERY cycle. Say so once and leave it alone.
        _pkg()._log.info("okf: %s knowledge is at the %d-char cap — %d newer "
                  "fact(s) not carried", topic, _BODY_CHARS, len(fresh))
        return 0, dropped
    meta = dict(node.get("meta") or {})
    meta.setdefault("scope", _topic_scope(topic))
    meta.setdefault("category", topic)
    ok = _store.save_node("learning", nid, meta, body, reindex=False).get("ok")
    return (1 if ok else 0), dropped


def sync_briefs_to_nodes() -> dict:
    """Turn this machine's compacted briefs into OKF nodes, and keep them current.

    **This is what makes local compaction reach the other machines.** Briefs are
    class A files that stay local by design (each machine compacts its own), so
    the only thing that travels is OKF knowledge — and a fact that never became
    a node never leaves this box. Running this on every cycle closes that gap.

    Deterministic and idempotent: one global learning node per brief topic,
    body = the union of that topic's facts, newest appended. A topic already
    represented is UPDATED rather than skipped — the one-shot
    :func:`migrate_from_briefs` skips it, which is right for a migration and
    wrong for a cycle, because every fact added after the first run would be
    invisible forever.

    No LLM is involved: the distillation already happened when the brief was
    written, and re-summarising here would be a second, non-deterministic fold
    of the same text on every machine.
    """
    facts_by_topic = _brief_facts_by_topic()
    if not facts_by_topic:
        return {"ok": True, "created": 0, "updated": 0, "topics": 0}

    existing = _learning_by_topic(_graph.build(force=True))
    created = updated = dropped = 0
    for topic, facts in facts_by_topic.items():
        held = existing.get(topic.lower())
        if held is None:
            n, d = _create_topic_node(topic, facts)
            created += n
        else:
            n, d = _update_topic_node(topic, facts, held)
            updated += n
        dropped += d

    if created or updated:
        _store._write_index()          # one rewrite for the whole pass
    _pkg()._log.info("okf: briefs → nodes created=%d updated=%d dropped=%d over %d topic(s)",
              created, updated, dropped, len(facts_by_topic))
    return {"ok": True, "created": created, "updated": updated,
            "dropped": dropped, "topics": len(facts_by_topic)}


__all__ = ["extract_and_save", "write_session_node", "migrate_from_briefs",
           "sync_briefs_to_nodes",
           "record_solution", "reclassify_global_learnings",
           "record_repo_profile", "record_script", "record_task",
           "build_repo_profiles"]
