"""What the request says the test mocks must be what the code calls.

A request that states "its `ReactiveMongoTemplate mongoTemplate` field mocked
for `findById(id, org.bson.Document.class, collectionName)`, `getConverter()`
and `save(org.bson.Document, collectionName)`" tells exactly which calls the
hidden test stubs. A real run wrote `findById(id, Settings.class, name)`
instead: unstubbed, the mock returned null and both tests died with a
NullPointerException while the model's own tests (mocking what it wrote)
passed. This check reads the lines the turn ADDED to non-test code and lists
each mocked call it never makes, or makes without the class the request names.
"""
from __future__ import annotations

import os
import re
import subprocess

_MOCK_WORD = re.compile(r"\b(?:mock(?:ed|s)?|stub(?:bed|s)?)\b", re.I)
_TICK = re.compile(r"`([^`\n]{3,300})`")
_CALL = re.compile(r"^\s*(?:[\w$]+\.)*([a-z_][\w$]*)\s*\((.*)\)\s*;?\s*$", re.S)
_CLASS_LIT = re.compile(r"\b(?:[\w$]+\.)*([A-Z][\w$]*)(?:\.class|::class)\b")
_TEST_PATH = re.compile(r"(?:^|/)(?:src/test|tests?|__tests__|spec)/|(?:Test|Tests|Spec|_test|_spec)\.\w+$"
                        r"|(?:^|/)test_[^/]*$")
_CODE_EXT = (".java", ".kt", ".kts", ".scala", ".cs", ".groovy", ".py", ".ts", ".js", ".go", ".rs")


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_MOCK_CONTRACT", "1").strip().lower() not in (
        "0", "false", "no", "off")


def claims(request: str) -> list:
    """``[(call text, method name, [class names])]`` for each backticked call
    after a "mocked"/"stubbed" word in the same sentence."""
    out, seen = [], set()
    for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z`])|\n+", request or ""):
        m = _MOCK_WORD.search(sent)
        if not m:
            continue
        for t in _TICK.finditer(sent, m.end()):
            c = _CALL.match(t.group(1))
            if not c:
                continue
            name = c.group(1)
            classes = sorted(set(_CLASS_LIT.findall(c.group(2))))
            key = (name, tuple(classes))
            if key in seen:
                continue
            seen.add(key)
            out.append((t.group(1).strip(), name, classes))
    return out


def _added_code(root: str, base: "str | None") -> str:
    """The lines this work added to non-test source files (vs ``base``, else
    HEAD), plus new untracked source files whole."""
    def git(*a):
        r = subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, timeout=20)
        return r.stdout if r.returncode == 0 else ""
    rev = base or "HEAD"
    parts = []
    diff = git("diff", "-U0", rev, "--", ".")
    cur = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("+") and cur.endswith(_CODE_EXT) and not _TEST_PATH.search(cur):
            parts.append(line[1:])
    for p in git("ls-files", "--others", "--exclude-standard").splitlines():
        if p.endswith(_CODE_EXT) and not _TEST_PATH.search(p):
            try:
                with open(os.path.join(root, p), encoding="utf-8", errors="replace") as fh:
                    parts.append(fh.read(400_000))
            except OSError:
                pass
    return "\n".join(parts)


def missing(request: str, root: str, base: "str | None" = None) -> list:
    """One gap line per mocked call the added code does not make as stated.
    [] when the request names no mocked calls or nothing was added."""
    if not enabled():
        return []
    cl = claims(request)
    if not cl or not root or not os.path.isdir(root):
        return []
    try:
        code = _added_code(root, base)
    except Exception:  # noqa: BLE001 — never block on git
        return []
    if not code.strip():
        return []
    gaps = []
    for text, name, classes in cl:
        calls = [m.start() for m in re.finditer(rf"\b{re.escape(name)}\s*\(", code)]
        if not calls:
            gaps.append(f"`{text}` — the request says the test mocks this call, and the "
                        f"new code never calls {name}(…); an unmocked call returns null in the test")
            continue
        for cls in classes:
            if not re.search(rf"\b{re.escape(cls)}(?:\.class|::class)\b", code):
                gaps.append(f"`{text}` — the test mocks {name} with {cls}.class; the new code "
                            f"calls {name} without it, so the mock does not match and returns null")
    return gaps


__all__ = ["claims", "missing", "enabled"]
