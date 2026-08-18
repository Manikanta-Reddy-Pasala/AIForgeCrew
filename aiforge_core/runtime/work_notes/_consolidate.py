"""Intelligent LLM-backed consolidation (map+dedupe → OKR sections)."""
from __future__ import annotations

import datetime as _dt
import json
import os
import re

from aiforge_core.config import _atomic

from ._helpers import _as_items, _log, _now_iso
from ._render import parse_note, render_note


# ── intelligent consolidation (LLM map+dedupe → OKR sections) ─────────────
#
# update_note is a DUMB whole-section replace: the caller must hand it the full
# merged list or it clobbers. consolidate() is the smart write — it folds NEW
# free-form knowledge INTO the existing OKR sections with an LLM that dedupes
# paraphrases, resolves contradictions (newer supersedes), and MAPS each item to
# the right section. Large input is cut on STRUCTURE boundaries (chonkie) and
# folded chunk-by-chunk so nothing is sliced mid-fact. Soft: any LLM/adapter
# failure degrades to a deterministic union+dedupe merge — never raises, never
# loses the new content.

def _okf_rules() -> str:
    """The OKF v0.1 producer rules (single source: memory.okf) — appended to the
    consolidation prompt so the compacted note stays an OKF-valid concept."""
    try:
        from aiforge_core.memory.okf import OKF_RULES
        return OKF_RULES
    except Exception:  # noqa: BLE001
        return ""


def _supersede_directive() -> str:
    """The contradiction-handling rule for consolidation — config-driven via
    ``AIFORGE_OKR_SUPERSEDE`` (``archive`` | ``keep``). ``archive`` (default)
    drops the stale line (OKR cycle-close; git history keeps the old value);
    ``keep`` tags it ``[superseded <date>]`` and keeps both (a visible
    retrospective trail in the brief). Read per call so the env is live."""
    if os.environ.get("AIFORGE_OKR_SUPERSEDE", "archive").strip().lower() == "keep":
        today = _dt.datetime.now(_dt.UTC).date().isoformat()
        return (f"SUPERSEDE: when new info contradicts an old line, KEEP BOTH — "
                f"append ' [superseded {today}]' to the stale line and add the "
                f"new value as a fresh line; never delete the old value.")
    return ("SUPERSEDE / UPDATE-IN-PLACE: when new info changes a fact's value "
            "(a number, status, owner, path, decision, config value), REPLACE the "
            "fact with the LATEST value — emit ONLY the current value as ONE line. "
            "DROP the old value entirely: do NOT keep it as a second line and do "
            "NOT annotate it with '(old)', '(previously X)', '(was …)', "
            "'(new)', or '[superseded]'. Each fact states the CURRENT truth, not "
            "its history.")


_CONSOLIDATE_SYS = (
    "You maintain a knowledge note in Google-OKR format. You are given the "
    "note's CURRENT sections (JSON) and NEW information. Produce the CONSOLIDATED "
    "sections.\n"
    "Rules:\n"
    "- DEDUPE: merge paraphrases/near-duplicates into one crisp line; never emit "
    "two lines saying the same thing.\n"
    "- MAP each item to the correct section: Objective = the one-line goal; "
    "Key Results = measurable outcomes/targets AND tickets worked — a jira/issue "
    "key (e.g. PROJ-123) IS a Key Result (the concrete measurable work) and its "
    "reference is ALSO copied into Links; Facts = stable truths, config, points "
    "to remember, current state; Links = URLs / cross-references (COPY VERBATIM, "
    "never reword or invent); Learnings = discoveries, gotchas, dated changes.\n"
    "- Keep every item ONE concise sentence. Do NOT invent facts not present in "
    "the inputs. Preserve existing content unless a rule above removes it.\n"
    "\n"
    + _okf_rules()
)


def _ci_key(s: str) -> str:
    """Case/space-insensitive dedupe key for a section item."""
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


_JUNK_ITEM_RE = re.compile(
    r"^(?:#{1,6}\s|-{3,}\s*$|_source:|_gathered\b|```|<!--)", re.I)


def _dedupe_ci(items) -> list[str]:
    """Order-preserving dedupe of a section's items. Beyond exact (case/space-
    insensitive) dupes it drops a shorter item fully CONTAINED in a longer kept
    one (the common near-dupe: "status: Done" vs "status: Done (auto)") and
    strips obvious junk lines (markdown headers/rules/fences, source markers)
    that leak in when a raw blob is folded without an LLM."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for it in _as_items(items):
        s = str(it).strip()
        if not s or _JUNK_ITEM_RE.match(s):
            continue
        k = _ci_key(s)
        if k and k not in seen:
            seen.add(k)
            cleaned.append(s)
    # containment pass: drop any item whose text is a substring of a longer one
    out: list[str] = []
    keys = [_ci_key(c) for c in cleaned]
    for i, c in enumerate(cleaned):
        ki = keys[i]
        if any(i != j and ki in keys[j] and len(keys[j]) > len(ki)
               for j in range(len(cleaned))):
            continue
        out.append(c)
    return out


def _sections_dict(objective="", key_results=None, facts=None, links=None,
                   learnings=None) -> dict:
    return {"objective": (objective or "").strip(),
            "key_results": _as_items(key_results), "facts": _as_items(facts),
            "links": _as_items(links), "learnings": _as_items(learnings)}


def _deterministic_merge(existing: dict, new_content: str) -> dict:
    """No-LLM fallback: append the new content as a single deduped Fact and
    dedupe every section. Never loses information, never reorders history."""
    out = {
        "objective": (existing.get("objective") or "").strip(),
        "key_results": _dedupe_ci(existing.get("key_results")),
        "facts": _dedupe_ci(existing.get("facts")),
        "links": _dedupe_ci(existing.get("links")),
        "learnings": _dedupe_ci(existing.get("learnings")),
    }
    fact = re.sub(r"\s+", " ", (new_content or "").strip())
    if fact and _ci_key(fact) not in {_ci_key(f) for f in out["facts"]}:
        out["facts"].append(fact)
    return out



# ── context budgeting ─────────────────────────────────────────────────────
#
# A fold sends {current_sections, new_information} and asks for the WHOLE
# consolidated note back. Both halves grow: `current_sections` accumulates every
# fact kept so far, and the output request was sized from that same payload. On
# a 2,163-node mesh fold this reached 229,377 input tokens with 32,768 output
# requested against a 262,144 window — 262,145 total, over by ONE token, and
# every retry failed identically because nothing shrank.
#
# So both sides are budgeted here, against the real window:
#   * what we SEND is capped, and whatever does not fit is held back and merged
#     deterministically afterwards — bounded call, no lost facts;
#   * what we ASK FOR is whatever the window has left, never a fixed ceiling.

# Chars per token. Deliberately pessimistic (real English is ~4): a fold that
# under-estimates its prompt gets a 400 and loses the whole group, while one
# that over-estimates just does an extra chunk.
_CHARS_PER_TOKEN = 3

# Leave room for the system prompt, the JSON scaffolding and tokeniser
# disagreement. The failure above was a ONE-token overshoot, so the slack is
# what stops arithmetic that is merely almost right.
_CTX_SLACK_TOKENS = 2048

# Smallest useful completion. Below this the model cannot return even a trimmed
# note, so the caller must shrink the input instead of making a doomed call.
_MIN_OUTPUT_TOKENS = 1024


def _ctx_window(role: str) -> int:
    """Context window for ``role``, in tokens.

    Asks the router, which already resolves per-role env → runtime settings →
    default, so the operator's configured window is honoured rather than a
    second constant drifting alongside it.
    """
    import os as _os

    raw = (_os.environ.get("AIFORGE_CONSOLIDATE_CTX_TOKENS") or "").strip()
    if raw:
        try:
            return max(8192, int(raw))
        except ValueError:
            pass
    try:
        from aiforge_core.llm.router import _local_ctx_window
        return max(8192, int(_local_ctx_window(role)))
    except Exception:  # noqa: BLE001 — a missing router must not stop compaction
        return 32768


def _est_tokens(text: str) -> int:
    return max(1, len(text or "") // _CHARS_PER_TOKEN)


def _sections_chars(sections: dict) -> int:
    return len(json.dumps(sections, ensure_ascii=False))


def _split_state(sections: dict, budget_chars: int) -> tuple[dict, dict]:
    """Split accumulated sections into (sent, held) under ``budget_chars``.

    The model needs the accumulated state to dedupe against, but it does not
    need ALL of it to fold one chunk — and sending all of it is what blew the
    window. Items are kept newest-first (the tail of each list is what the most
    recent folds added, i.e. what the next chunk is most likely to duplicate);
    everything beyond the budget is held back and re-unioned by the caller, so
    this bounds the CALL without dropping a single fact.
    """
    sent = {"objective": sections.get("objective") or "",
            "key_results": [], "facts": [], "links": [], "learnings": []}
    held = {"key_results": [], "facts": [], "links": [], "learnings": []}
    used = len(sent["objective"])
    # Round-robin across sections so one huge list cannot starve the others.
    pools = {k: list(reversed(sections.get(k) or [])) for k in held}
    while any(pools.values()):
        for k in ("facts", "learnings", "key_results", "links"):
            if not pools[k]:
                continue
            item = pools[k].pop(0)
            cost = len(str(item)) + 4
            if used + cost > budget_chars:
                held[k].append(item)
            else:
                sent[k].append(item)
                used += cost
    for k in held:
        sent[k].reverse()          # restore chronological order for the prompt
        held[k].reverse()
    return sent, held


def _union_exact(first, second) -> list[str]:
    """``first`` then whatever of ``second`` it does not already contain.

    Exact (case/space-insensitive) keys only — deliberately NOT ``_dedupe_ci``.
    That one also drops a short item CONTAINED in a longer one, which is right
    when distilling a section but wrong here: this runs over items that were
    already deduped once, and re-applying containment across the join silently
    ate survivors ("a" vanishing because "brand new fact" contains an "a").
    Merging back what the model never saw must add, never subtract.
    """
    out = list(first or [])
    seen = {_ci_key(str(x)) for x in out}
    for item in (second or []):
        k = _ci_key(str(item))
        if k and k not in seen:
            seen.add(k)
            out.append(item)
    return out


def _union_sections(a: dict, b: dict) -> dict:
    """Union of two section dicts, ``a`` first, adding only what ``b`` uniquely
    holds. See :func:`_union_exact` for why this does not re-dedupe."""
    return {
        "objective": (a.get("objective") or b.get("objective") or "").strip(),
        "key_results": _union_exact(a.get("key_results"), b.get("key_results")),
        "facts": _union_exact(a.get("facts"), b.get("facts")),
        "links": _union_exact(a.get("links"), b.get("links")),
        "learnings": _union_exact(a.get("learnings"), b.get("learnings")),
    }


def _consolidate_once(existing: dict, new_content: str, role: str) -> dict:
    """One structured LLM fold: (existing sections + new_content) → consolidated
    sections. Returns the deterministic merge on ANY failure."""
    from pydantic import BaseModel

    class ConsolidatedNote(BaseModel):
        objective: str = ""
        key_results: list[str] = []
        facts: list[str] = []
        links: list[str] = []
        learnings: list[str] = []

    payload = json.dumps({"current_sections": existing,
                          "new_information": new_content}, ensure_ascii=False)
    try:
        import os as _os
        from aiforge_core.llm.structured import structured_complete
        # The consolidated JSON re-emits EVERY accumulated fact each fold, so a
        # fixed output cap truncates a fact-heavy brief (IncompleteOutputException
        # → fallback loop). A dedupe-fold never EXPANDS its input, so size the
        # output budget from the actual payload (≈chars/3 tokens + slack), clamped
        # to a ceiling. Dynamic, not a magic constant; override the ceiling with
        # AIFORGE_CONSOLIDATE_MAX_TOKENS.
        _cap = int(_os.environ.get("AIFORGE_CONSOLIDATE_MAX_TOKENS", "32768"))
        _mt = max(4096, min(_cap, len(payload) // 3 + 1024))
        # …and then cut to what the WINDOW actually has left. Sizing the output
        # from the payload alone is what produced a 229,377-token prompt asking
        # for 32,768 more against a 262,144 window: correct arithmetic about the
        # wrong quantity. input + output must fit, so output is what remains.
        _window = _ctx_window(role)
        _room = _window - _est_tokens(payload) - _CTX_SLACK_TOKENS
        if _room < _MIN_OUTPUT_TOKENS:
            # The prompt itself does not leave room for a usable answer. Folding
            # it would 400 every time, so fall back deterministically instead of
            # burning a call and a retry to learn that.
            _log.warning("consolidate: %d-token prompt leaves no room in a "
                         "%d-token window — deterministic merge for this chunk",
                         _est_tokens(payload), _window)
            return _deterministic_merge(existing, new_content)
        _mt = max(_MIN_OUTPUT_TOKENS, min(_mt, _room))
        # Inject the supersede directive at call time (env is live) so the
        # contradiction policy (archive vs keep) is honoured per run.
        sys_prompt = _CONSOLIDATE_SYS + "\n- " + _supersede_directive()
        res = structured_complete(
            role,
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": payload}],
            ConsolidatedNote, max_retries=1, max_tokens=_mt, temperature=0.1)
        return {"objective": (res.objective or "").strip(),
                "key_results": _dedupe_ci(res.key_results),
                "facts": _dedupe_ci(res.facts),
                # links pass through normalize_links at render time; dedupe here
                "links": _dedupe_ci(res.links),
                "learnings": _dedupe_ci(res.learnings)}
    except Exception:  # noqa: BLE001 — model down / bad JSON → deterministic
        return _deterministic_merge(existing, new_content)


def consolidate(existing: dict, new_content: str, *, role: str = "learner",
                max_input_chars: int | None = None, label: str | None = None) -> dict:
    """Fold ``new_content`` into ``existing`` OKR sections via an LLM that
    dedupes, resolves contradictions, and maps each item to its section.

    ``existing`` is a sections dict (objective:str, the rest lists — missing
    keys tolerated). Large ``new_content`` is cut on STRUCTURE boundaries
    (chonkie) and folded chunk-by-chunk. Returns a consolidated sections dict;
    degrades to a deterministic union+dedupe merge if no model is reachable.

    ``max_input_chars`` is the total per-call input window (existing JSON + one
    chunk). Defaults from AIFORGE_CONSOLIDATE_INPUT_CHARS (48000) — modern
    long-context models swallow that whole, and a conservative 12k window made
    a large brief collapse to a 1k budget → dozens of tiny chunks (slow + poor
    distillation)."""
    import os as _os
    if max_input_chars is None:
        max_input_chars = int(
            _os.environ.get("AIFORGE_CONSOLIDATE_INPUT_CHARS", "48000"))
    cur = _sections_dict(**{k: existing.get(k) for k in
                            ("objective", "key_results", "facts", "links",
                             "learnings") if k in existing}) \
        if existing else _sections_dict()
    text = (new_content or "").strip()
    if not text:
        # nothing new — just normalize/dedupe the existing sections (no LLM)
        return {"objective": cur["objective"],
                "key_results": _dedupe_ci(cur["key_results"]),
                "facts": _dedupe_ci(cur["facts"]),
                "links": _dedupe_ci(cur["links"]),
                "learnings": _dedupe_ci(cur["learnings"])}

    # Budget the per-call input against the REAL window, not just the configured
    # char cap. `max_input_chars` bounds one chunk; the window bounds
    # chunk + accumulated state + system prompt together, and it is that sum
    # which was overflowing.
    window_chars = max(0, (_ctx_window(role) - _CTX_SLACK_TOKENS
                           - _MIN_OUTPUT_TOKENS)) * _CHARS_PER_TOKEN
    max_input_chars = min(max_input_chars, window_chars) or max_input_chars
    # How much of the accumulated state may ride along on each call. The rest is
    # held back and merged deterministically, so this bounds the request without
    # dropping facts. Half the window by default: enough context to dedupe
    # against, never so much that the state alone fills the prompt.
    state_chars = int(_os.environ.get("AIFORGE_CONSOLIDATE_STATE_CHARS",
                                      str(max(8000, max_input_chars // 2))))
    reserve = min(_sections_chars(cur), state_chars)
    # Never collapse to a sliver: a big existing brief must still get a usable
    # chunk budget (else 25k of new text becomes 27 folds). Floor at 8k.
    budget = max(8000, max_input_chars - reserve)
    chunks: list[str]
    if len(text) <= budget:
        chunks = [text]
    else:
        try:
            from aiforge_core.integrations import chonkie_text_adapter as _ck
            if _ck.available():
                # structure-aware chunks under budget; fold each in turn
                chunks = []
                buf, used = [], 0
                for part in _ck.chunk_text(text, chunk_tokens=max(64, budget // 8)):
                    if used and used + len(part) > budget:
                        chunks.append("".join(buf))
                        buf, used = [], 0
                    buf.append(part)
                    used += len(part)
                if buf:
                    chunks.append("".join(buf))
            else:
                chunks = [text[i:i + budget] for i in range(0, len(text), budget)]
        except Exception:  # noqa: BLE001 — chunker down → plain slices
            chunks = [text[i:i + budget] for i in range(0, len(text), budget)]

    # One-line visibility into every compaction: which scope, how many source
    # chars, how many chonkie chunks it was folded through, into one brief.
    _log.info("compact %s: %d chars → %d chunk(s) → 1 brief",
              label or f"role={role}", len(text), len(chunks))
    if len(chunks) > 1:
        _log.info("consolidate: folding %d chunk(s) (%d chars) via LLM…",
                  len(chunks), len(text))
    for _ci, ch in enumerate(chunks, 1):
        if len(chunks) > 1:
            _log.info("consolidate: chunk %d/%d (%d chars)…", _ci, len(chunks), len(ch))
        # Send a BOUNDED slice of what we have so far; hold the rest back and
        # union it into the answer. Without this the accumulator grew with every
        # chunk until the prompt no longer fit — the later chunks of a big group
        # could never succeed, so the whole fold degraded.
        sent, held = _split_state(cur, state_chars)
        if any(held.get(k) for k in ("facts", "learnings", "key_results", "links")):
            _log.info("consolidate: state %d chars over the %d-char call budget "
                      "— folding against a slice, merging the rest locally",
                      _sections_chars(cur), state_chars)
        cur = _union_sections(_consolidate_once(sent, ch, role), held)
    return cur


def consolidate_note(path: str, new_content: str, *, role: str = "learner",
                     kind: str | None = None, key: str | None = None) -> dict:
    """Read-modify-write ``path``: intelligently fold ``new_content`` into the
    note's OKR sections (LLM map+dedupe, chonkie for big input) and rewrite in
    OKR format — preserving unknown sections + free body. Soft-error contract:
    returns ``{"ok", ...}``, never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return {"ok": False, "error": f"read failed: {exc}"}
    parsed = parse_note(text)
    fm, sec = parsed["frontmatter"], parsed["sections"]
    k = str(kind or fm.get("kind") or "misc")
    key_ = str(key or fm.get("key") or "unknown")
    merged = consolidate(sec, new_content, role=role)
    rendered = render_note(
        k, key_, title=parsed["title"] or key_,
        source_url=str(fm.get("source_url") or ""),
        objective=merged["objective"], key_results=merged["key_results"],
        facts=merged["facts"], links=merged["links"],
        learnings=merged["learnings"], body_md=parsed["body"],
        updated_at=_now_iso(), tags=fm.get("tags"))
    try:
        _atomic.write_text(path, rendered)
    except OSError as exc:
        return {"ok": False, "error": f"write failed: {exc}"}
    return {"ok": True, "path": path}
