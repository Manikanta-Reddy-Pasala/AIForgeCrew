"""Finish gate: what the request spells out (runtime/spec_contract) must be
in the code before the turn ends on "done".

Deterministic and cheap (ast over the work folder), so it runs before the
model is asked whether the request is done (_done_check). A run that said
"all parts completed" with `receive` lacking `expiry`, no `return_items` and
no invoices module is sent back with that list. Bounded: after the send-backs
the turn may end, and the answer names what is still missing.
"""
from __future__ import annotations

import re

# The words of a gap line itself, never a name.
_GAP_WORDS = {"does", "take", "missing", "class", "found", "function", "module",
              "exist", "defined"}

#: Send-backs in a row (reset once the model acts), and per turn.
_SEND_BACKS = 2
_SEND_BACKS_TOTAL = 3
_SHOWN = 25


def _gaps(st, cached: bool = True) -> list:
    from aiforge_core.runtime import spec_contract
    goal = str(getattr(st, "goal", "") or "")
    cwd = getattr(st, "cwd", None)
    if not goal or not cwd:
        return []
    try:
        # A shell write (sed -i, git mv) changes files without an edit count:
        # the mid-run note reads the tree fresh.
        key = (id(st), len(getattr(st, "convo", None) or []),
               getattr(st, "edits_made", 0)) if cached else None
        gaps = spec_contract.missing(goal, str(cwd), key=key)
    except Exception:  # noqa: BLE001 — the check never blocks an answer
        return []
    # What the user said mid-run wins over the first message: a gap whose
    # names a steer mentions ("skip the invoices module") is theirs to drop.
    said = " ".join(str(s) for s in (getattr(st, "steers", None) or [])).lower()
    if said:
        gaps = [g for g in gaps if not any(
            w.lower() in said for w in re.findall(r"[A-Za-z_]\w{3,}", g)
            if w.lower() not in _GAP_WORDS)]
    return gaps


_WAIVE = re.compile(r"not required:\s*`?(\.?/?[A-Za-z_][\w./-]{2,})", re.I)
_NAME = re.compile(r"[A-Za-z_][\w./-]*")


def _gap_names(gap: str) -> set:
    """The names a gap line is about: "Cart.total is missing" → {Cart.total,
    total}; never its own words."""
    out = set()
    for t in _NAME.findall(gap):
        t = t.rstrip(".")
        if t.lower() in _GAP_WORDS or len(t) < 3:
            continue
        out.add(t)
        if "." in t and not t.endswith(".py"):
            out.add(t.rsplit(".", 1)[-1])
    return out


def _note_waivers(st, text: str, gaps: "list | None" = None) -> None:
    """``not required: Cart.legacy_tax`` in an answer: the model says the
    request means that name must not exist. Only a name some current gap is
    exactly about counts ("no", "a", "get" waive nothing they are not)."""
    names = {m.group(1).rstrip(".").lstrip("./") or m.group(1) for m in _WAIVE.finditer(text or "")}
    if not names:
        return
    known = set().union(*(_gap_names(g) for g in (gaps or []))) if gaps else set()
    names &= known
    if names:
        st.contract_waived = set(getattr(st, "contract_waived", None) or ()) | names


def _waived(st, gaps: list) -> list:
    w = getattr(st, "contract_waived", None) or ()
    if not w:
        return gaps
    return [g for g in gaps if not (_gap_names(g) & set(w))]


def _checked(st) -> int:
    """How many names, signatures and files the request spells out (what
    the check looked for in the code)."""
    from aiforge_core.runtime import spec_contract
    try:
        return len(spec_contract.claims(str(getattr(st, "goal", "") or "")))
    except Exception:  # noqa: BLE001
        return 0


def message(gaps: list) -> str:
    more = f"\n- … and {len(gaps) - _SHOWN} more" if len(gaps) > _SHOWN else ""
    return ("[automated spec check — not the user] The request spells out these "
            "names and signatures, and the code does not have them yet:\n"
            + "\n".join(f"- {g}" for g in gaps[:_SHOWN]) + more + "\n"
            "The request is not done. Implement each one exactly as the request "
            "writes it (same names, same parameters), then run the tests. Your "
            "own tests passing does not count when the API differs from the request.\n"
            "Only if the request means one of these must NOT exist (it is removed, "
            "renamed away, or only shown as an example), put a line "
            "`not required: <name>` for it in your answer instead.")


def gate(st, step, builder=None, strict_finish=False):
    """Returns ``"continue"`` when the code lacks what the request names.
    Not for a strict-finish run (a subtask whose goal may carry the whole
    spec, with other parts done elsewhere) — like the done check."""
    if builder or strict_finish or getattr(st, "readonly_mode", False) \
            or getattr(st, "plan_mode", False):
        return None
    if not getattr(st, "edits_made", 0):
        return None          # a question, a review: nothing was built
    found = _gaps(st)
    _note_waivers(st, step.get("text") or "", found)
    gaps = _waived(st, found)
    st.contract_gaps = gaps
    # A waived gap is the model's word, not a pass: the model done check
    # (which skips a run whose names all checked out) still runs.
    st.contract_checked = 0 if len(gaps) < len(found) else _checked(st)
    if not gaps:
        return None
    from ._finish import _budget_left
    if not _budget_left(st, "contract_nudges", _SEND_BACKS, per_turn=_SEND_BACKS_TOTAL):
        return None
    if step.get("text"):
        yield {"type": "thought", "text": step["text"]}
    yield {"type": "thought", "role": "system",
           "text": f"▶ {len(gaps)} thing(s) the request names are not in the code — continuing"}
    st.convo.append({"role": "user", "content": message(gaps)})
    return "continue"


__all__ = ["gate", "message"]


def progress_note(st) -> "str | None":
    """After a condense, mid-run: what the request names that the code still
    lacks, said once per change of that list. The finish gate only ran when
    the model tried to finish; a run that never did (click af5, 51 minutes,
    ShellComplete defined where the request's module never exported it, 0/37)
    heard nothing. Advisory: it never ends or blocks a step."""
    if getattr(st, "readonly_mode", False) or getattr(st, "plan_mode", False) \
            or not getattr(st, "edits_made", 0):
        return None
    gaps = _waived(st, _gaps(st, cached=False))
    key = tuple(gaps)
    if not gaps or getattr(st, "contract_progress_said", None) == key:
        return None
    st.contract_progress_said = key
    more = f"\n- … and {len(gaps) - _SHOWN} more" if len(gaps) > _SHOWN else ""
    return ("[automated spec check — not the user] Progress check: the request "
            "names these and the code does not have them yet:\n"
            + "\n".join(f"- {g}" for g in gaps[:_SHOWN]) + more + "\n"
            "Keep going with the plan; make sure each one ends up exactly where "
            "and as the request writes it (the module path it gives is the one "
            "that must export the name).")

