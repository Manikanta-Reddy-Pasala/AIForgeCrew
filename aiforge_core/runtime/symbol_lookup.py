"""A build that fails on a name the compiler cannot resolve: say at once
whether that type exists anywhere — in the project or a downloaded
dependency — and which real names are closest.

A Kotlin run imported a class that does not exist (an invented API name),
got "Unresolved reference", and spent 30+ calls grepping sources, build
files and the version catalog for it, then gave up. The answer was one
lookup away: the 465K classes of the 1,871 cached dependency jars index in
~2 s, and the project's own declarations come from its sources.

Only type names (capitalised) are looked up; a missing method or variable
is left to the compiler message. ``AIFORGE_CHAT_SYMBOL_LOOKUP=0`` turns it
off.
"""
from __future__ import annotations

import difflib
import os
import re
import subprocess
import threading
import time
import zipfile

#: Compiler messages naming an unresolved type, per language.
_UNRESOLVED = (
    re.compile(r"Unresolved reference:?\s*'?`?([A-Z][\w$]*)"),                   # kotlin
    re.compile(r"cannot find symbol[^\n]*\n[^\n]*symbol:\s+(?:class|interface|variable)\s+([A-Z][\w$]*)"),  # javac
    re.compile(r"\bpackage ([\w.]+) does not exist"),                              # javac import
    re.compile(r"cannot find (?:type|struct|trait|value) `([A-Z][\w]*)`"),         # rustc
    re.compile(r"unresolved import `(?:[\w:]+::)?([A-Z][\w]*)`"),                  # rustc
    re.compile(r"Cannot find name '([A-Z][\w$]*)'"),                               # tsc
    re.compile(r"\bundefined: (?:\w+\.)?([A-Z]\w*)"),                              # go
    re.compile(r"\bunable to resolve class ([A-Z][\w$]*)"),                         # groovy
)
_JAR_ROOTS = ("~/.gradle/caches/modules-2/files-2.1", "~/.m2/repository")
_SRC_EXT = (".java", ".kt", ".kts", ".scala", ".groovy", ".rs", ".ts", ".tsx", ".go", ".cs")
_DECL = re.compile(r"\b(?:class|interface|object|enum|record|struct|trait|type|typealias)\s+([A-Z][\w$]*)")
_MAX_NAMES = 4
_INDEX_TTL_S = 1800

_LOCK = threading.Lock()
_JARS: dict = {"at": 0.0, "names": {}}          # simple name -> fully qualified (first seen)
_SRC: dict = {}                                  # project root -> (at, {simple: rel path})


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_SYMBOL_LOOKUP", "1").strip().lower() not in (
        "0", "false", "no", "off")


def unresolved(text: str) -> list:
    """The type names a build output says it could not resolve, in order."""
    out = []
    for rx in _UNRESOLVED:
        for m in rx.finditer(text or ""):
            name = m.group(1).rsplit(".", 1)[-1] if "." in m.group(1) else m.group(1)
            if name and name[0].isupper() and name not in out:
                out.append(name)
    return out[:_MAX_NAMES]


def _jar_index() -> dict:
    with _LOCK:
        if _JARS["names"] and time.time() - _JARS["at"] < _INDEX_TTL_S:
            return _JARS["names"]
        names: dict = {}
        t0 = time.time()
        for root in _JAR_ROOTS:
            root = os.path.expanduser(root)
            for dp, _dn, fn in os.walk(root):
                for f in fn:
                    if not f.endswith(".jar") or f.endswith(("-sources.jar", "-javadoc.jar")):
                        continue
                    try:
                        with zipfile.ZipFile(os.path.join(dp, f)) as z:
                            for e in z.namelist():
                                if e.endswith(".class") and "$" not in e:
                                    fq = e[:-6].replace("/", ".")
                                    names.setdefault(fq.rsplit(".", 1)[-1], fq)
                    except Exception:  # noqa: BLE001 — a bad jar is skipped
                        continue
                if time.time() - t0 > 30:          # bounded: a huge cache gets a partial index
                    break
        _JARS.update(at=time.time(), names=names)
        return names


def _src_index(root: str) -> dict:
    hit = _SRC.get(root)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    names: dict = {}
    try:
        r = subprocess.run(["git", "-C", root, "ls-files", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, text=True, timeout=20)
        files = [f for f in r.stdout.splitlines() if f.endswith(_SRC_EXT)][:20000]
    except Exception:  # noqa: BLE001
        files = []
    for rel in files:
        try:
            with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
                for m in _DECL.finditer(fh.read(300_000)):
                    names.setdefault(m.group(1), rel)
        except OSError:
            continue
    _SRC[root] = (time.time(), names)
    return names


_WORDS = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+")


def _closest(name: str, pool, n: int = 6) -> list:
    """Real names sharing at least half of ``name``'s words (CamelCase),
    best character match first: `KtPropertyAccessExpression` ->
    KtQualifiedExpression, KtDotQualifiedExpression, …"""
    words = set(_WORDS.findall(name))
    need = max(1, (len(words) + 1) // 2)
    cand = [x for x in pool if x != name and len(words & set(_WORDS.findall(x))) >= need]
    first = (_WORDS.findall(name) or [""])[0]
    # The same API family first (same leading word: Kt…, Psi…), then likeness.
    cand.sort(key=lambda x: (not x.startswith(first),
                             -len(words & set(_WORDS.findall(x))),
                             -difflib.SequenceMatcher(None, name, x).ratio()))
    return cand[:n]


def note(text: str, root: str, jvm: bool = True) -> "str | None":
    """One line per unresolved type: where it exists, or that it does not and
    the closest real names. None when the output names none."""
    if not enabled():
        return None
    names = unresolved(text)
    if not names:
        return None
    try:
        src = _src_index(root) if root and os.path.isdir(root) else {}
        jars = _jar_index() if jvm else {}
    except Exception:  # noqa: BLE001 — a lookup never breaks the command result
        return None
    lines = []
    pool = None
    for n in names:
        if n in src:
            lines.append(f"`{n}` exists in this project ({src[n]}): import or qualify it there.")
        elif n in jars:
            lines.append(f"`{n}` exists in a dependency as `{jars[n]}`: import it.")
        else:
            if pool is None:
                pool = set(src) | set(jars)
            close = _closest(n, pool)
            where = "this project or any downloaded dependency" if jvm else "this project"
            lines.append(f"`{n}` does not exist in {where} — do not search for it further."
                         + (f" Closest real names: {', '.join(close)}." if close else ""))
    return "symbol check: " + " ".join(lines)


__all__ = ["note", "unresolved", "enabled"]
