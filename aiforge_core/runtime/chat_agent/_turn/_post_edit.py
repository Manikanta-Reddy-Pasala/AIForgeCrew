"""After an edit lands in a chat turn: format the files, then hand the model
the errors in them (runtime/post_edit does the work; this is the turn's side —
what is said in the chat and in the conversation)."""
from __future__ import annotations

import re

from aiforge_core.runtime.tools.mutating import writes_files

from ._progress import _full, _named_paths

# "m.py:12:5: F821 …" / "a.ts(3,1): error …": the place, which moves when
# lines above change; the same error further down is not new.
_PLACE = re.compile(r"^.*?(?::\d+:\d+:|\(\d+,\d+\):)\s*")


def _quiet(fn, *args):
    """A formatter or checker never ends a turn."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        return None


def edited_files(name, args, result, cwd) -> list:
    """(as named, absolute) for each file a landed write touched — resolved
    the way the progress bookkeeping does (_progress.note_write)."""
    if not writes_files(name, args if isinstance(args, dict) else {}):
        return []
    if isinstance(result, dict) and result.get("ok") is False:
        return []
    named = _named_paths(args, None) or _named_paths(None, result)
    return [(p, _full(p, cwd)) for p in dict.fromkeys(named)]


def post_edit_format(st, files, cwd):
    """Format the files just written with the project's formatter. When that
    changed one, the model's copy is stale: say so, so its next patch reads
    the file first. Runs BEFORE the write is hashed, so the progress state
    sees the file as it stays."""
    from aiforge_core.runtime import post_edit
    for shown, full in files:
        fmt = _quiet(post_edit.format_file, full, cwd)
        if fmt:
            yield {"type": "thought", "role": "system",
                   "text": f"Formatted {shown} with {fmt}"}
            st.convo.append({"role": "user", "content":
                f"[automated formatter — not the user] {fmt} reformatted {shown} "
                "after your edit. Read the lines again before you patch this file."})


def post_edit_diagnostics(st, files, cwd):
    """Errors (never style) in the files just written, handed to the model
    this step. The same errors (wherever the lines moved) are said once; a
    check that could not run says nothing and forgets nothing."""
    from aiforge_core.runtime import post_edit
    said = getattr(st, "post_edit_said", None)
    if said is None:
        said = st.post_edit_said = {}
    for shown, full in files:
        errs = _quiet(post_edit.diagnostics, full, cwd)
        if errs is None:
            continue
        if not errs:
            said.pop(full, None)
            continue
        key = sorted(_PLACE.sub("", e) for e in errs)
        if said.get(full) == key:
            continue
        said[full] = key
        yield {"type": "thought", "role": "system",
               "text": f"⚠ {len(errs)} error(s) in {shown} — fix them now: {errs[0][:160]}"}
        st.convo.append({"role": "user", "content":
            "[automated error check — not the user] The file you just wrote has "
            f"these errors; fix them before continuing:\n" + "\n".join(errs)[:1500]})


__all__ = ["edited_files", "post_edit_diagnostics", "post_edit_format"]
