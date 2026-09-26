"""What a failing test, build or command run failed ON — as a fingerprint.

Every repair loop in the runtime asks the same question: "is this the same
failure I saw after the last fix?". Comparing raw output never says yes —
timings, temp paths, addresses and line numbers move on every run — and
comparing only pytest ``FAILED`` lines says nothing at all for a Maven,
Gradle, Jest, Go or compiler failure. :func:`failure_of` reads the output of
any of them and returns:

* ``signature`` — the failing test ids, or the compiler errors (file +
  message, no line/column), or, when neither is there, the first error line;
  normalised so the same failure reads the same on every run. ``""`` when
  the output names no failure at all;
* ``count`` — how many distinct failures that is (the progress measure);
* ``headline`` — a short readable form for a message to the model or user.

:func:`runner_green` is the other half: True only when the output carries a
test runner's OWN success evidence ("12 passed", "BUILD SUCCESS", "ok pkg").
"""
from __future__ import annotations

import re
from typing import NamedTuple

#: Failing items kept in one signature; a larger set is still one signature.
_MAX_ITEMS = 40
_MAX_HEADLINE = 200


class Failure(NamedTuple):
    signature: str
    count: int
    headline: str


NO_FAILURE = Failure("", 0, "")

# ── normalisation ────────────────────────────────────────────────────────

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_TEMP = re.compile(r"(?:/private)?/(?:tmp|var/folders|var/tmp)/\S*"
                   r"|\S*pytest-of-[^/\s]+/pytest-\d+/\S*"
                   r"|[A-Za-z]:\\\S*\\Temp\\\S*")
_TIMING = re.compile(
    r"\(\s*\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds)\s*\)"
    r"|\b(?:in|took|after)\s+\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds|m|min)\b"
    r"|Time elapsed:\s*[\d.,]+\s*(?:s|sec|ms)\b"
    r"|\b\d+(?:\.\d+)?(?:ms|s)\b", re.I)
_CLOCK = re.compile(r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?Z?\b"
                    r"|\b\d\d:\d\d:\d\d(?:[.,]\d+)?\b")
_LINE_COL = re.compile(r":\[\d+,\d+\]|\(\d+,\d+\)|:\d+(?::\d+)?(?=[:\s)\]]|$)"
                       r"|\bline \d+\b", re.I)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")
_SPACE = re.compile(r"\s+")


def _clean(text: str) -> str:
    """Strip what differs between two runs of the same failure. Applied to
    a message, never to the whole output: the patterns that find a failure
    need the timings and paths still in place."""
    text = _ANSI.sub("", text)
    text = _TEMP.sub("<tmp>", text)
    text = _UUID.sub("<id>", text)
    text = _HEX.sub("<addr>", text)
    text = _CLOCK.sub("", text)
    return _TIMING.sub("", text)


def _message(text: str) -> str:
    """A normalised error message: also no line numbers or loose numbers,
    so ``assert 3 == 4`` and ``assert 5 == 4`` read as one failure."""
    text = _LINE_COL.sub("", _clean(text))
    text = _NUMBER.sub("N", text)
    return _SPACE.sub(" ", text).strip(" :-")[:_MAX_HEADLINE]


def _short_path(path: str) -> str:
    """The last two parts of a path: a worktree or temp prefix differs
    between attempts, the file does not."""
    path = path.replace("\\", "/").strip("'\"")
    if path.startswith("file://"):
        path = path[7:]
    parts = [p for p in path.split("/") if p and p != "."]
    return "/".join(parts[-2:])


# ── test runners: failing test ids ───────────────────────────────────────

_PYTEST = re.compile(r"^(?:FAILED|ERROR)[ \t]+(\S+?)(?:[ \t]+-[ \t].*)?$", re.M)
_PYTEST_COLLECT = re.compile(r"ERROR collecting (\S+)")
_SUREFIRE_CLASS = re.compile(
    r"Tests run:.*?<<<\s*(?:FAILURE|ERROR)!.*?\bin\s+([\w.$]+)\s*$", re.M)
_SUREFIRE_METHOD = re.compile(
    r"^(?:\[ERROR\]\s+)?([\w.$]+)\s+--\s+Time elapsed.*?<<<\s*(?:FAILURE|ERROR)!",
    re.M)
_SUREFIRE_LISTED = re.compile(
    r"^\[ERROR\]\s{1,6}([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)+)(?::\d+)?(?:\s|$)",
    re.M)
_GRADLE = re.compile(r"^(\S[^\n>]*?)[ \t]>[ \t]([^\n]+?)[ \t]+FAILED[ \t]*$", re.M)
_JEST_CASE = re.compile(r"^[ \t]*(?:✕|×|✗)[ \t]+(.+?)[ \t]*$", re.M)
_JEST_FILE = re.compile(r"^[ \t]*FAIL[ \t]+(\S+)(?:[ \t]+>[ \t]+(.+?))?[ \t]*$", re.M)
_GO_CASE = re.compile(r"^[ \t]*--- FAIL:[ \t]+(\S+)", re.M)


def _test_items(text: str) -> list[str]:
    items: list[str] = []
    items += [f"test:{m}" for m in _PYTEST.findall(text)]
    items += [f"test:{m}" for m in _PYTEST_COLLECT.findall(text)]
    items += [f"junit:{m}" for m in _SUREFIRE_CLASS.findall(text)]
    items += [f"junit:{m}" for m in _SUREFIRE_METHOD.findall(text)]
    items += [f"junit:{m}" for m in _SUREFIRE_LISTED.findall(text)
              if not m.lower().startswith(("org.apache.maven", "tests run"))]
    items += [f"gradle:{c.strip()} > {n.strip()}" for c, n in _GRADLE.findall(text)]
    items += [f"js:{_message(m)}" for m in _JEST_CASE.findall(text)]
    items += [f"js:{f}" + (f" > {_message(n)}" if n else "")
              for f, n in _JEST_FILE.findall(text)]
    items += [f"go:{m}" for m in _GO_CASE.findall(text)]
    return items


# ── compilers: file + message, no line or column ─────────────────────────

_COMPILERS = (
    # javac / gcc / clang: Foo.java:12: error: cannot find symbol
    re.compile(r"^(\S+?\.\w{1,6}):\d+(?::\d+)?:\s+(?:fatal\s+)?error:\s*(.+)$", re.M),
    # maven: [ERROR] /x/Foo.java:[12,5] cannot find symbol
    re.compile(r"^\[ERROR\]\s+(\S+?\.(?:java|kt|scala|groovy)):\[\d+,\d+\]\s+(.+)$", re.M),
    # tsc: src/a.ts(12,5): error TS2322: …   and   src/a.ts:12:5 - error TS2322: …
    re.compile(r"^(\S+?\.[cm]?[jt]sx?)\(\d+,\d+\):\s+error\s+(TS\d+:.+)$", re.M),
    re.compile(r"^(\S+?\.[cm]?[jt]sx?):\d+:\d+\s+-\s+error\s+(TS\d+:.+)$", re.M),
    # go build / vet: ./main.go:12:5: undefined: foo
    re.compile(r"^(\S+?\.go):\d+:\d+:\s+(.+)$", re.M),
    # kotlin: e: file:///x/Foo.kt:12:5 Unresolved reference: x
    re.compile(r"^e:\s+(\S+?\.kts?):\d+:\d+\s+(.+)$", re.M),
)
# rustc: error[E0425]: cannot find value `x`\n  --> src/main.rs:3:5
_RUSTC = re.compile(r"^error(\[E\d+\])?:\s+(.+)\n\s*-->\s*(\S+?):\d+:\d+", re.M)


def _compile_items(text: str) -> list[str]:
    items = [f"build:{_short_path(f)}: {_message(m)}"
             for rx in _COMPILERS for f, m in rx.findall(text)]
    items += [f"build:{_short_path(f)}: {code}{_message(m)}"
              for code, m, f in _RUSTC.findall(text)]
    return items


# ── last resorts: a traceback, npm, the first error line ─────────────────

_TRACE_FRAME = re.compile(r'^\s*File "([^"]+)", line \d+, in (\S+)', re.M)
_EXC_LINE = re.compile(
    r"^((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Exit|Interrupt|Warning))"
    r"(?::\s*(.*))?$", re.M)
_NPM = re.compile(r"^npm (?:ERR!|error)\s+(.+)$", re.M)
_NPM_NOISE = re.compile(r"complete log of this run|^code\s|^errno\s|^path\s|"
                        r"^syscall\s|^command\s|^signal\s|^cwd\s|^\s*$|"
                        r"^A complete log|^Log files were not", re.I)
_ERROR_WORD = re.compile(
    r"\b(error|errors|failed|failure|exception|fatal|cannot|undefined|"
    r"not found|no such)\b", re.I)
#: Summary lines that mention errors but report none.
_ZERO_SUMMARY = re.compile(r"\b0 (?:failed|errors?|failures?)\b|Failures:\s*0,\s*Errors:\s*0"
                           r"|\berrors?:\s*0\b|\bno errors?\b", re.I)


def _traceback_item(text: str) -> str:
    if "Traceback (most recent call last)" not in text:
        return ""
    excs = _EXC_LINE.findall(text)
    if not excs:
        return ""
    name, msg = excs[-1]
    frames = _TRACE_FRAME.findall(text)
    where = (f"{_short_path(frames[-1][0])}:{frames[-1][1]}: " if frames else "")
    return f"py:{where}{name}: {_message(msg)}".rstrip(": ")


def _npm_items(text: str) -> list[str]:
    lines = [ln for ln in _NPM.findall(text) if not _NPM_NOISE.search(ln)]
    return [f"npm:{_message(ln)}" for ln in lines[:2]]


def _first_error_line(text: str) -> str:
    for line in text.splitlines():
        if _ERROR_WORD.search(line) and not _ZERO_SUMMARY.search(line):
            msg = _message(line)
            if msg:
                return msg
    return ""


# ── the public API ───────────────────────────────────────────────────────

def failure_of(output) -> Failure:
    """The failure a test/build/command output describes, or ``NO_FAILURE``.

    Test ids come first (the precise answer); then compiler errors; then a
    Python traceback or npm error; then the first line that reads as an
    error. Empty output never has a signature."""
    text = _ANSI.sub("", str(output or ""))
    if not text.strip():
        return NO_FAILURE
    items = _dedupe(_test_items(text)) or _dedupe(_compile_items(text))
    if not items:
        tb = _traceback_item(text)
        items = [tb] if tb else _dedupe(_npm_items(text))
    if items:
        kept = sorted(items)[:_MAX_ITEMS]
        return Failure("|".join(kept), len(items), "; ".join(kept[:3])[:_MAX_HEADLINE])
    line = _first_error_line(text)
    if not line:
        return NO_FAILURE
    return Failure(f"line:{line}", 1, line)


def signature(output) -> str:
    """Just the fingerprint; ``""`` when the output names no failure."""
    return failure_of(output).signature


def signature_of(*parts) -> str:
    """The signature of several texts or tool results read as one output —
    an attempt's own error, its run result and its validation."""
    return signature("\n".join(result_text(p) if isinstance(p, dict) else str(p or "")
                                for p in parts))


def _dedupe(items: list[str]) -> list[str]:
    seen: set = set()
    return [i for i in items if i and not (i in seen or seen.add(i))]


def result_text(result) -> str:
    """The text a tool result carries: stdout, stderr, output and error."""
    if not isinstance(result, dict):
        return str(result or "")
    parts = [result.get(k) for k in ("stdout", "stderr", "output", "error", "detail")]
    return "\n".join(str(p) for p in parts if isinstance(p, str) and p)


# ── success evidence ─────────────────────────────────────────────────────

_GREEN = (
    re.compile(r"\b\d+ passed\b"),                                # pytest
    re.compile(r"^Tests:\s.*\b\d+ passed\b", re.M),               # jest
    re.compile(r"^\s*Tests\s+\d+ passed", re.M),                  # vitest
    re.compile(r"\bBUILD SUCCESS(?:FUL)?\b"),                     # maven/gradle
    re.compile(r"^ok\s+\S+", re.M),                               # go test
    re.compile(r"\btest result: ok\.", re.M),                     # cargo
    re.compile(r"^OK(?: \(|$)", re.M),                            # unittest
)
_RED = re.compile(r"\b[1-9]\d* (?:failed|errors?|failing)\b|\bFAILED\b|"
                  r"\bBUILD FAILURE\b|\bBUILD FAILED\b|^FAIL\b|"
                  r"Failures:\s*[1-9]|Errors:\s*[1-9]|test result: FAILED", re.M)


def runner_green(output) -> bool:
    """True only when a test runner said, in its own words, that it passed
    and nothing in the output says a test failed."""
    text = _ANSI.sub("", str(output or ""))
    return (any(rx.search(text) for rx in _GREEN) and not _RED.search(text))


__all__ = ["Failure", "NO_FAILURE", "failure_of", "signature", "signature_of",
           "result_text", "runner_green"]
