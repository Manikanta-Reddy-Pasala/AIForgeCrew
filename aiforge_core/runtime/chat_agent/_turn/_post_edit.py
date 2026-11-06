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
        hints = _quiet(_where_defined, errs, full) or []
        st.convo.append({"role": "user", "content":
            "[automated error check — not the user] The file you just wrote has "
            f"these errors; fix them before continuing:\n" + "\n".join(errs)[:1500]
            + ("\n" + "\n".join(hints) if hints else "")})


_UNDEFINED = re.compile(r"F821 Undefined name [`'\"]?(\w+)")
_ASIDE = re.compile(r"(?:^|/)(?:tests?|docs?|examples?|benchmarks?|scripts|vendor|_vendor|"
                    r"\.worktrees|build|dist|site-packages)/")


def _module_of(path: str) -> str:
    """The dotted module a file is, from its package folders (``__init__.py``
    up the tree); a file outside a package is its own name."""
    import os
    parts = [os.path.splitext(os.path.basename(path))[0]]
    d = os.path.dirname(path)
    while os.path.isfile(os.path.join(d, "__init__.py")):
        parts.insert(0, os.path.basename(d))
        d = os.path.dirname(d)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _where_defined(errs, full) -> list:
    """For an undefined name the project defines (``class`` / ``def``) in one
    clear place elsewhere: where, and how to import it. click a3 got
    "Undefined name CompletionItem" in types.py seven times; the class lives
    in shell_completion.py, which imports types — the import has to sit
    inside the function that uses it. A name defined in several places, or
    only in tests/docs/vendored copies, gets no hint (a wrong one is worse)."""
    import os
    import subprocess
    names = list(dict.fromkeys(m.group(1) for e in errs if (m := _UNDEFINED.search(e))))[:5]
    if not names:
        return []
    full = os.path.realpath(full)
    top = subprocess.run(["git", "-C", os.path.dirname(full), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=4)
    if top.returncode != 0:
        return []
    root = os.path.realpath(top.stdout.strip())
    me = os.path.relpath(full, root).replace(os.sep, "/")
    alt = "|".join(names)
    hit = subprocess.run(
        ["git", "-C", root, "grep", "--untracked", "-z", "-n", "-E",
         rf"^(class|def|async def) ({alt})([^A-Za-z0-9_]|$)", "--", "*.py"],
        capture_output=True, text=True, timeout=4)
    found: dict = {}
    for row in hit.stdout.split("\n"):
        bits = row.split("\0")
        if len(bits) < 3 or bits[0] == me or _ASIDE.search(bits[0]):
            continue
        m = re.match(r"(?:class|def|async def) (\w+)", bits[2])
        if m and m.group(1) in names:
            found.setdefault(m.group(1), []).append(bits[0])
    my_top = me.split("/")[0]
    my_mod = _module_of(full).rsplit(".", 1)[-1]
    out = []
    for name in names:
        where = sorted(set(found.get(name) or []),
                       key=lambda p: (p.split("/")[0] != my_top, p.count("/"), p))
        if not where or len(where) > 2:
            continue                  # nowhere, or no one clear owner
        src = where[0]
        mod = _module_of(os.path.join(root, src))
        try:
            with open(os.path.join(root, src), encoding="utf-8", errors="replace") as fh:
                text = fh.read(200_000)
            cycle = bool(re.search(rf"^\s*(?:from\s+[\w.]*\s+import\s+[^\n]*\b{my_mod}\b|"
                                   rf"from\s+[\w.]*\b{my_mod}\s+import|import\s+[\w.]*\b{my_mod}\b)",
                                   text, re.M))
        except OSError:
            cycle = False
        how = ("that module imports this file, so put the import inside the function "
               "that uses it (a top-level import would be circular)."
               if cycle else "import it at the top of this file.")
        out.append(f"Hint: `{name}` is defined in {src} (module `{mod}`); {how}")
    return out


__all__ = ["edited_files", "post_edit_diagnostics", "post_edit_format"]
