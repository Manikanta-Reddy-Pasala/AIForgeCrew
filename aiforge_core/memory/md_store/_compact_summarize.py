"""Summarising notes to fit the model, and labelling them with topics."""
from __future__ import annotations

import os
import re


def _pkg():
    """The package, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.memory.md_store._compact as package
    return package


_SECTION_SEP = "\n\n---\n\n"  # blank-line-fenced markdown section separator

# Sentinel topic key for a note the topic labeller couldn't theme. Such a note
# already lives in its repo/shared brief, so it must NOT spawn a topic file —
# and it must NEVER fall back to the note's KIND (that minted the junk
# compacted-learning.md / compacted-user-comment.md briefs). `compact()` drops
# this group before writing.
_NO_TOPIC = "\x00no-topic"


_SUMMARY_SYS = (
    "You consolidate engineering memory notes. Merge the notes below into ONE "
    "concise markdown document. Deduplicate ruthlessly, group related points "
    "under '## ' section headings, and KEEP every concrete fact, decision, "
    "gotcha, file path, command, id and number. Drop chit-chat, repetition and "
    "filler. Do not invent anything. Output ONLY the markdown body — no preamble, "
    "no surrounding code fence."
)
# Upper bound on the notes sent to ONE summarize call. A ceiling, not the
# budget: the real limit is whatever the role's context window leaves after the
# completion (``_summary_input_cap``). A bare constant is a guess about a
# window rather than a reading of it — too small for a 262k model (needless
# map-reduce passes over text that would fit in one) and too large the moment
# a role points at a 32k one, which is how a fold ends up 400ing on length.
_SUMMARY_INPUT_CAP = 28_000
_COMPACT_BODY_CAP = 60_000      # max chars of a deterministic-merge consolidated
                                # file (bounds growth when no model is reachable)


_SUMMARY_OUT_TOKENS = 4096      # a brief, not a book


def _summary_input_cap(role: str) -> int:
    """Chars of notes one summarize call may carry, for THIS role's window.

    Shares the rule with the OKR fold (``work_notes._consolidate``) rather than
    keeping a second constant that drifts against it.
    """
    try:
        from aiforge_core.runtime.work_notes._consolidate import input_char_budget
        return max(4000, min(_SUMMARY_INPUT_CAP,
                             input_char_budget(role, output_tokens=_SUMMARY_OUT_TOKENS)))
    except Exception:  # noqa: BLE001 — budgeting must never break compaction
        return _SUMMARY_INPUT_CAP


def _summarize_block(text: str, role: str) -> str | None:
    """One LLM consolidation call. Returns markdown, or None on any failure
    (model down / unknown role / empty) so the caller falls back to merge."""
    cap = _summary_input_cap(role)
    if len(text) > cap:
        # Refused rather than sent: an oversized block is what produces a
        # context-length 400, and a 400 fails the WHOLE compaction (this
        # function returning None bails the entire op to a deterministic
        # merge). The caller splits and retries instead.
        return None
    try:
        from aiforge_core.llm.client import complete
        out = complete(
            role,
            [{"role": "system", "content": _SUMMARY_SYS},
             {"role": "user", "content": text}],
            temperature=0.2, max_tokens=_SUMMARY_OUT_TOKENS,
        )
    except Exception:  # noqa: BLE001 — any failure → deterministic merge
        return None
    out = (out or "").strip()
    # strip an accidental wrapping ```/```md fence
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z0-9]*\s*\n?", "", out)
        out = re.sub(r"\n?```\s*$", "", out).strip()
    return out or None


def _split_to_cap(block: str, cap: int) -> list[str]:
    """Split ONE oversized block on line boundaries so every piece fits.

    A single block can exceed the cap on its own — one enormous capture, a
    pasted log. Batching alone never split it, so it went to the model whole,
    400'd on length, and bailed the whole compaction.
    """
    if len(block) <= cap:
        return [block]
    out, buf, used = [], [], 0
    for line in (block or "").splitlines(keepends=True):
        if used and used + len(line) > cap:
            out.append("".join(buf))
            buf, used = [], 0
        buf.append(line)
        used += len(line)
    if buf:
        out.append("".join(buf))
    return out


def _batch_under_cap(blocks: list[str], cap: int) -> list[str]:
    """Greedily batch blocks under the input cap."""
    batches: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for b in blocks:
        if cur and cur_len + len(b) > cap:
            batches.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(b)
        cur_len += len(b)
    if cur:
        batches.append("\n\n".join(cur))
    return batches


def _summarize_notes(blocks: list[str], role: str) -> str | None:
    """Map-reduce consolidation: summarize the notes (batched to fit the input
    cap), then summarize the partial summaries if there was more than one
    batch. Returns markdown or None (→ caller merges deterministically)."""
    if not blocks:
        return None
    cap = _summary_input_cap(role)
    sized = [piece for b in blocks for piece in _split_to_cap(b, cap)]
    partials: list[str] = []
    for batch in _batch_under_cap(sized, cap):
        s = _pkg()._summarize_block(batch, role)
        if s is None:
            return None          # bail whole op → deterministic merge
        partials.append(s)
    if len(partials) == 1:
        return partials[0]
    # reduce step — combine the partial summaries
    combined = _SECTION_SEP.join(partials)
    if len(combined) <= cap:
        return _pkg()._summarize_block(combined, role) or combined
    return combined          # already summarized; accept as-is if still huge


def _snap_known_topics(files, known):
    """Deterministically snap each note to the nearest known topic over the cutoff (no LLM). Returns (labels, leftover_notes, shortlist_of_nearest)."""
    from . import _topics
    labels: dict = {}
    leftover: list[dict] = []
    shortlist: list[str] = []
    vec_cache: dict = {}

    for d in files:
        text = (d.get("title") or "") + "\n" + (d.get("body") or "")[:400]
        hit, near = _topics.snap_by_similarity(text, known, _cache=vec_cache)
        if not shortlist and near:
            shortlist = near
        if hit:
            labels[d["file"]] = hit
        else:
            leftover.append(d)
    return labels, leftover, shortlist


def _topic_labels(files: list[dict], role: str) -> dict:
    """Map ``{file_name: topic-slug}`` for a compaction pass.

    Deterministic FIRST: each note is scored against the topic briefs already
    on disk and snapped to the nearest one over the cutoff — no LLM, no drift,
    and the vocabulary stays stable across runs. Only the notes with no home
    reach the model, and it sees just those titles plus a shortlist of the
    nearest existing topics (never all ~140), because handing the model the
    whole vocabulary is itself a drift source.

    Every slug — snapped or invented — then passes admission control
    (:func:`_topics.admit`), so generic magnets (``code``/``data``/``tmp``) and
    junk (``m``/``na2``) never mint a file. Returns ``{}`` on total failure;
    the caller falls back to kind grouping.
    """
    if not files:
        return {}
    from . import _topics
    from ._scope import _snap_topic

    known = _topics.existing_topics()
    labels, leftover, shortlist = _snap_known_topics(files, known)

    if leftover:
        for f, slug in _llm_topic_labels(leftover, role, shortlist).items():
            labels[f] = slug

    out: dict = {}
    for f, slug in labels.items():
        ok = _topics.admit(slug, _snap_topic)
        if ok:
            out[f] = ok
    return out


# One labelling call's worth of titles. The listing used to be sliced to this
# and sent as a single call, so on a box with a backlog only the first ~50 notes
# were ever themed: the rest stayed un-topic'd, were never archived, and were
# re-read (and re-truncated) on every pass — a queue that could not drain.
_LABEL_LISTING_CAP = 4000


def _label_batches(files: list[dict], cap: int) -> list[list[dict]]:
    """``files`` split into groups whose listing fits one call."""
    batches: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for d in files:
        cost = len((d.get("title") or d.get("file") or "")[:80]) + 8
        if cur and size + cost > cap:
            batches.append(cur)
            cur, size = [], 0
        cur.append(d)
        size += cost
    if cur:
        batches.append(cur)
    return batches


def _llm_topic_labels(files: list[dict], role: str,
                      shortlist: list[str]) -> dict:
    """Theme every leftover note, in as many calls as it takes."""
    labels: dict = {}
    for batch in _label_batches(files, _LABEL_LISTING_CAP):
        labels.update(_llm_topic_labels_once(batch, role, shortlist))
    return labels


def _llm_topic_labels_once(files: list[dict], role: str,
                           shortlist: list[str]) -> dict:
    """Ask the model to theme ONLY the notes the deterministic snap could not
    place, choosing from ``shortlist`` when one fits. Small payload by design:
    the leftover titles and at most a dozen candidate topics. Empty on any
    failure — those notes then stay in their repo/shared brief."""
    if len(files) < 1:
        return {}
    listing = "\n".join(f"{i}: {(d.get('title') or d.get('file') or '')[:80]}"
                        for i, d in enumerate(files))
    known = ("\nEXISTING TOPICS (prefer one of these): "
             + ", ".join(shortlist)) if shortlist else ""
    try:
        from pydantic import RootModel

        from aiforge_core.llm.structured import structured_complete

        class _Topics(RootModel[dict]):
            pass

        raw = structured_complete(role, [
            {"role": "system", "content":
             "Assign each memory-note title a SUBJECT topic. Reuse an existing "
             "topic whenever it fits; only invent a slug when none does. A new "
             "slug must name a real subject (2-4 words, kebab-case) — never a "
             "generic word like code/data/file/test/build and never an "
             "abbreviation. Reply ONLY a JSON object mapping each index (as a "
             'string) to its topic slug, e.g. {"0":"data-sync"}. '
             "Every index must appear once." + known},
            {"role": "user", "content": listing[:_LABEL_LISTING_CAP]},
        ], _Topics, max_tokens=600, max_retries=1, temperature=0.0).root
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    import re as _re
    labels: dict = {}
    for k, v in raw.items():
        try:
            idx = int(k)
        except (ValueError, TypeError):
            continue
        if 0 <= idx < len(files) and isinstance(v, str) and v.strip():
            slug = _re.sub(r"[^a-z0-9]+", "-", v.strip().lower()).strip("-")[:40]
            if slug:
                labels[files[idx]["file"]] = slug
    return labels


def _repo_key(d: dict) -> str:
    """Project-brief axis: one consolidated file per repo. An explicit
    frontmatter ``repo``, else a ``repo:<x>`` tag, else "shared" (cross-repo)."""
    if d.get("repo"):
        return d["repo"]
    for t in d.get("tags") or []:
        if t.startswith("repo:"):
            return t.split(":", 1)[1] or "shared"
    return "shared"


def _topic_key(d: dict) -> str:
    """Topic axis: an explicit frontmatter ``topic`` or a ``topic:<slug>`` tag
    wins (no LLM needed); else the precomputed label; else UNGROUPED.

    A note the labeller couldn't theme returns _NO_TOPIC (dropped by compact())
    — it must NOT fall back to the note's KIND, which minted junk briefs like
    compacted-learning.md / compacted-user-comment.md.
    """
    if d.get("topic"):
        return d["topic"]
    for t in d.get("tags") or []:
        if t.startswith("topic:"):
            return t.split(":", 1)[1] or _NO_TOPIC
    return d.get("_topic") or _NO_TOPIC


def _group_key(d: dict, group_by: str) -> str:
    if group_by == "repo":
        return _repo_key(d)
    if group_by == "topic":
        return _topic_key(d)
    if group_by == "tag":
        return (d["tags"][0] if d.get("tags") else "untagged")
    if group_by == "source":
        # the leading token of the source key (e.g. "chat", "md", "ticket")
        return (d.get("source") or "manual").split(":", 1)[0].split("-", 1)[0]
    return d.get("kind") or "note"


def _topic_split_cap() -> int:
    """Facts-size (chars) beyond which a topic brief SPLITS into linked parts.
    A major topic that outgrows this becomes compacted-<topic>.md +
    compacted-<topic>-2.md … cross-referenced. Env AIFORGE_TOPIC_SPLIT_CAP."""
    try:
        return max(500, int(os.environ.get("AIFORGE_TOPIC_SPLIT_CAP", "12000")))
    except (TypeError, ValueError):
        return 12000
