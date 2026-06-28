"""Gap M6 — cold-fact rollup / summarization (memory bound).

Memory grows unbounded: every observation lives forever and the vector
recall + PPR reranker pay a linear cost on stale facts. This module
rolls old, topically-clustered observations into a single ``digest``
node so the working set stays small while the gist survives.

Public surface:
    select_cold(rows, *, now, cold_days=90, min_group=5) -> dict[str, list]
    build_digest(topic, rows, *, summarize=None)         -> dict
    run_digest(driver, *, repo, now, summarize=None, archive=True) -> dict

``select_cold`` and ``build_digest`` are pure (no I/O). ``run_digest``
orchestrates: it reads recent observations via
``store.list_memory``, groups the cold ones, writes one digest per group
back via ``store.upsert_observation``, and — only when the
``AIFORGE_DIGEST_ARCHIVE`` env flag is truthy — marks the contributing
source nodes ``status='archived'`` so they drop out of active recall.
Read-only by default (flag off) so a misfire can never lose data.
"""
from __future__ import annotations

import os

from aiforge_memory.features.memory import store

_DAY_SECONDS = 86400.0
_MAX_BULLETS = 5

_ARCHIVE_SOURCES = """
MATCH (n {repo:$repo})
WHERE n.id IN $ids
SET n.status = 'archived'
RETURN count(n) AS n
"""


def select_cold(
    rows: list[dict],
    *,
    now: float,
    cold_days: float = 90.0,
    min_group: int = 5,
) -> dict[str, list[dict]]:
    """Group cold observation rows by topic, keeping only sizeable groups.

    A row is *cold* when its age (``now - created_at_epoch``) exceeds
    ``cold_days``. The topic key is the row's first tag, falling back to
    its ``kind``. Only groups with ``>= min_group`` members are returned
    so a one-off stale fact is left alone.

    Rows are expected to carry ``created_at_epoch`` + ``tags`` + ``kind``
    + ``text``; missing keys degrade gracefully (treated as never-cold /
    untagged).
    """
    cutoff = cold_days * _DAY_SECONDS
    groups: dict[str, list[dict]] = {}
    for row in rows:
        created = row.get("created_at_epoch")
        if created is None:
            continue
        age = now - float(created)
        if age <= cutoff:
            continue
        tags = row.get("tags") or []
        topic = tags[0] if tags else (row.get("kind") or "misc")
        groups.setdefault(topic, []).append(row)
    return {t: rs for t, rs in groups.items() if len(rs) >= min_group}


def build_digest(topic: str, rows: list[dict], *, summarize=None) -> dict:
    """Produce a digest record summarizing ``rows`` under ``topic``.

    When a ``summarize(prompt) -> str`` callable is injected it is used
    to generate the summary text. Otherwise a deterministic fallback
    concatenates the first ``_MAX_BULLETS`` row texts as bullet lines.
    """
    source_ids = [r.get("id") for r in rows if r.get("id") is not None]
    texts = [(r.get("text") or "").strip() for r in rows]

    if summarize is not None:
        prompt = (
            f"Summarize these {len(texts)} memory facts about "
            f"'{topic}' into a concise digest:\n\n"
            + "\n".join(f"- {t}" for t in texts)
        )
        text = summarize(prompt)
    else:
        bullets = [f"- {t}" for t in texts[:_MAX_BULLETS] if t]
        text = "\n".join(bullets)

    return {
        "topic": topic,
        "text": text,
        "source_ids": source_ids,
        "count": len(rows),
        "kind": "digest",
    }


def run_digest(
    driver,
    *,
    repo: str,
    now: float,
    summarize=None,
    archive: bool = True,
) -> dict:
    """Roll cold observations for ``repo`` into per-topic digests.

    Reads recent observations, selects the cold/clustered ones, writes a
    digest node per group, and (when ``archive=True`` *and* the
    ``AIFORGE_DIGEST_ARCHIVE`` env flag is truthy) marks each source node
    ``status='archived'``. Returns ``{groups, digests, archived}`` counts.
    """
    rows = store.list_memory(
        driver, repo=repo, label="Observation_v2", limit=1000,
    )
    groups = select_cold(rows, now=now)

    archive_enabled = archive and os.environ.get(
        "AIFORGE_DIGEST_ARCHIVE", "0",
    ) not in ("", "0", "false", "False")

    digests = 0
    archived = 0
    for topic, group_rows in groups.items():
        record = build_digest(topic, group_rows, summarize=summarize)
        store.upsert_observation(
            driver,
            repo=repo,
            text=record["text"],
            kind="digest",
            tags=["digest", f"topic:{topic}"],
            dedupe=True,
        )
        digests += 1

        if archive_enabled:
            ids = record["source_ids"]
            if ids:
                with driver.session() as s:
                    s.run(_ARCHIVE_SOURCES, repo=repo, ids=ids).consume()
                archived += len(ids)

    return {"groups": len(groups), "digests": digests, "archived": archived}
