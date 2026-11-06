"""The request's own examples must be tests.

The rules say "an example's exact input and output is a test as written";
the model writes unit tests of its own and skips the request's examples.
click a1: 12/20 on one hidden file, 3/17 on the other — the request gave the
exact output of ``_CLI_COMPLETE=complete_zsh`` (``"plain\\na\\n_\\nplain\\nb\\n
bee\\n"``) and said the bash script contains ``_CLI_COMPLETE=complete_bash``;
no test of the turn had either, and both were wrong.

Deterministic. The example values are the request's backticked output
literals (a quoted string, a list of quoted items, ``NAME=value``), never in a
sentence about the current or wrong behaviour. Each is looked for, loosely
(quotes, spaces, escapes, trailing commas ignored; ``NAME`` and ``value``
side by side in any form), in the test files the turn wrote. Values right
after an output word ("->", "returns", "is", "contains", "the output is")
are EXPECTED: when the request asks for tests and some are missing, the
finish sends the answer back once. All of them are named, as advice, at the
halfway time check. Off: ``AIFORGE_CHAT_EXAMPLE_GATE=0``.

An example written as an input and its result (`` `[] ""` -> `[]` ``) is a
pair: a test holds it when one test function has both the input's parts and
the result. click a7: the request gave ``[] "" -> []`` (options only after a
dash); the tests checked ``"-"`` alone, the code listed options on an empty
word too, and 28 of 37 hidden tests failed on it.
"""
from __future__ import annotations

import os
import re
import subprocess

_SPAN = re.compile(r"`([^`\n]{3,200})`")
_QUOTED = re.compile(r"""^(["'])(?:\\.|(?!\1)[^\\])*\1$""")   # disjoint: no backtracking blow-up
_LISTED = re.compile(r"""^\[\s*["'].*["']\s*,?\s*\]$""")
_ASSIGN = re.compile(r"^([A-Z_][A-Z0-9_]{2,})=([\w.-]+)$")
_FILEISH = re.compile(r"""^["'][\w.*/-]+\.\w{1,5}["']$""")
#: Right before a value it is an expected result.
_OUTPUT_CUE = re.compile(r"(?:->|=>|==|\breturns?\b|\boutputs?\b|\bprints?\b|\bcontains?\b|"
                         r"\bshows?\b|\bgives?\b|\bis\b|\bare\b|\bshould\s+be\b|\bexpected\b|"
                         r"\bresults?\b|\byields?\b|\bbecomes?\b|\bequals?\b)[^`]{0,40}$", re.I)
#: A sentence about the code as it is (or a wrong output) holds no expectation.
_NOT_EXPECTED = re.compile(r"\b(?:currently|today|used\s+to|instead\s+of|was|were|wrong|bug|"
                           r"incorrect|broken|before\s+the\s+fix|no\s+longer)\b", re.I)
_ASKS_TESTS = re.compile(r"\b(?:add|write|with|include|cover|create|own)\b[^.\n]{0,40}\b"
                         r"(?:tests?|pytest|unit\s+tests?)\b", re.I)
#: Plainly asks for new tests ("add your own pytest tests"): only then is
#: "no test yet" a gap worth naming.
_ASKS_NEW_TESTS = re.compile(r"\b(?:add|write|create)\s+(?:\w+\s+){0,3}(?:tests?|unit\s+tests?)\b", re.I)
_NO_TESTS = re.compile(r"\b(?:no|don'?t|do\s+not|without|skip)\s+(?:\w+\s+){0,2}tests?\b", re.I)
_TEST_DIR = re.compile(r"(?:^|/)(?:tests?|spec|specs|__tests__|testdata|fixtures)/")
_TEST_NAME = re.compile(r"(?:^|/)test_|_test\.|\.test\.|\.spec\.|_spec\.rb$|Test\.(?:java|kt)$")
_SKIP_DIR = re.compile(r"(?:^|/)(?:node_modules|\.venv|venv|site-packages|\.git|dist|build)/")
_MAX_FILES, _MAX_BYTES = 200, 512_000
_SHOWN = 16
#: `input` -> `result`, the two spans side by side.
_PAIR = re.compile(r"`([^`\n]{1,160})`[ \t]*(?:->|=>)[ \t]*`([^`\n]{1,200})`")
#: A result that is a value, not a description (``(args, incomplete)``).
_LITERAL = re.compile(r"""["']|^[\[(]\s*[\])]$|^-?\d|^(?:True|False|None)$""")
#: Lines a call and its assert (or one parametrize row) fit in.
_WINDOW = 4
_CALL = re.compile(r"^([A-Za-z_][\w.]*)\((.*)\)$")
_BLOCK = re.compile(r"\n(?=[ \t]*(?:@|(?:async[ \t]+)?def[ \t]|class[ \t]))")
_DOCSTRING = re.compile(r'"""(?:[^"\\]|\\.|"(?!""))*"""|\'\'\'(?:[^\'\\]|\\.|\'(?!\'\'))*\'\'\'')


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_EXAMPLE_GATE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _norm(text: str) -> str:
    t = re.sub(r"""[\s"']""", "", text or "")
    return re.sub(r",(?=[\]\)])", "", t).replace("(", "[").replace(")", "]")


def _loose(text: str) -> str:
    """Escapes gone too: a triple-quoted block with real line breaks, or
    ``\\"`` against ``'`` quoting, still matches."""
    t = re.sub(r"\\[ntr]", "", text or "")
    return _norm(t.replace("\\", ""))


def _sentence(text: str, start: int, end: int) -> str:
    lo = max(text.rfind(c, 0, start) for c in ".;\n")
    his = [i for i in (text.find(c, end) for c in ".;\n") if i >= 0]
    return text[lo + 1:min(his) if his else len(text)]


def examples(request: str, expected_only: bool = False) -> list:
    """The request's example values, in order, each once. ``expected_only``:
    only those right after an output word."""
    text = request or ""
    out = []
    for m in _SPAN.finditer(text):
        v = m.group(1).strip()
        if "{" in v or "%" in v:
            continue                      # a format template, not a value
        if _QUOTED.match(v):
            if len(_norm(v)) < 4:
                continue                  # "\n", "_": too common to tell
        elif not (_LISTED.match(v) or _ASSIGN.match(v)):
            continue
        sent = _sentence(text, m.start(), m.end())
        if _NOT_EXPECTED.search(sent):
            continue
        cue = bool(_OUTPUT_CUE.search(text[max(0, m.start() - 60):m.start()]))
        if _FILEISH.match(v) and not cue:
            continue                      # a file name, not an output
        if expected_only and not cue:
            continue
        out.append(v)
    return list(dict.fromkeys(out))


def _pnorm(text: str) -> str:
    """Pair matching keeps the quotes (``""`` is the input), one kind of them,
    and the parentheses (``f()`` is no ``[]``)."""
    t = re.sub(r"\s", "", (text or "").replace("'", '"'))
    return re.sub(r",(?=[\])])", "", t)


def _parts(value: str) -> list:
    """The top-level pieces of an input: ``["-t", "a"] "-"`` is
    ``["-t","a"]`` and ``"-"``."""
    out, cur, depth, quote, esc = [], "", 0, "", False
    for ch in value or "":
        if quote:
            cur += ch
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote, cur = ch, cur + ch
        elif ch in "[(":
            depth, cur = depth + 1, cur + ch
        elif ch in "])":
            depth, cur = max(0, depth - 1), cur + ch
        elif depth == 0 and (ch.isspace() or ch == ","):
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return [p for p in (_pnorm(x) for x in out) if p]


def pairs(request: str) -> list:
    """The request's ``input -> result`` examples, as ``(input, result)``."""
    text = request or ""
    out = []
    for m in _PAIR.finditer(text):
        a, b = m.group(1).strip(), m.group(2).strip()
        if any(c in a + b for c in "{%") or not _LITERAL.search(b):
            continue
        if _NOT_EXPECTED.search(_sentence(text, m.start(), m.end())):
            continue
        out.append((a, b))
    return list(dict.fromkeys(out))


def _windows(text: str) -> list:
    """Every run of ``_WINDOW`` lines of the tests, docstrings gone, never
    across a ``def`` or a decorator (a parametrize table is its own block)."""
    out = []
    for block in _BLOCK.split(_DOCSTRING.sub("", text or "")):
        lines = block.splitlines()
        out += [_pnorm("\n".join(lines[i:i + _WINDOW]))
                for i in range(max(1, len(lines) - _WINDOW + 1))]
    return out


def _take(part: str, text: str) -> "str | None":
    """``text`` with ``part`` taken out once, or None. An empty string is a
    whole argument (``f([], "")``), not the end of ``"x"`` or a docstring."""
    if part == '""':
        m = re.search(r'(?<=[,(\[=])""(?=[,)\]])', text)
        return None if m is None else text[:m.start()] + text[m.end():]
    i = text.find(part)
    return None if i < 0 else text[:i] + text[i + len(part):]


def _pair_held(a: str, b: str, windows: list) -> bool:
    """One window holds every part of the input and, apart from them, the
    result (``[] ""`` -> ``[]``: the ``[]`` argument is not the result)."""
    parts, want = [], _pnorm(b)
    for p in _parts(a):
        call = _CALL.match(p)
        # a call is its name and its own arguments, however the test spells
        # the rest (a parametrize table, keywords, another variable)
        parts += ([call.group(1)] + _parts(call.group(2))) if call else [p]
    for w in windows:
        rest = w
        for p in parts:
            rest = _take(p, rest) if rest is not None else None
        if rest is not None and _take(want, rest) is not None:
            return True
    return False


def _is_test_path(n: str) -> bool:
    if _SKIP_DIR.search(n):
        return False
    return bool(_TEST_DIR.search(n) or _TEST_NAME.search(n))


def _changed_tests(st, cwd: str, base: str) -> "str | None":
    """The text of the test files this turn wrote: those its tools wrote
    (``file_hashes``) and, from git, those changed since ``base`` or new since
    the turn began. None when git cannot tell (never a false gap)."""
    paths = {os.path.realpath(p) for p in (getattr(st, "file_hashes", None) or {})
             if _is_test_path(p.replace(os.sep, "/"))}
    try:
        top = subprocess.run(["git", "--no-optional-locks", "-C", cwd, "rev-parse",
                              "--show-toplevel"], capture_output=True, text=True, timeout=15)
        if top.returncode != 0:
            return None
        root = top.stdout.strip()

        def git(*a):
            return subprocess.run(["git", "--no-optional-locks", "-C", root, *a],
                                  capture_output=True, text=True, timeout=15)
        diff = git("diff", "--name-only", "--no-relative", "-z", base)
        if diff.returncode != 0:
            return None
        names = [n for n in diff.stdout.split("\0") if n]
        before = getattr(st, "example_untracked0", None) or set()
        new = git("ls-files", "--others", "--exclude-standard", "-z")
        names += [n for n in new.stdout.split("\0") if n and n not in before]
        paths |= {os.path.realpath(os.path.join(root, n)) for n in names if _is_test_path(n)}
    except Exception:  # noqa: BLE001
        return None
    if paths and not any(os.path.isfile(p) and os.path.getsize(p) <= _MAX_BYTES
                         for p in sorted(paths)[:_MAX_FILES]):
        return None                       # tests there, none readable: cannot tell
    texts = []
    for p in sorted(paths)[:_MAX_FILES]:
        try:
            if os.path.getsize(p) > _MAX_BYTES:
                continue
            with open(p, encoding="utf-8", errors="replace") as fh:
                texts.append(fh.read())
        except OSError:
            continue
    return "\n".join(texts)


def note_untracked(st, cwd) -> None:
    """At the turn's start: the untracked files already there (not its tests)."""
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", str(cwd), "ls-files", "--others",
                            "--exclude-standard", "--full-name", "-z"],
                           capture_output=True, text=True, timeout=15)
        st.example_untracked0 = {n for n in r.stdout.split("\0") if n} if r.returncode == 0 \
            else set()
    except Exception:  # noqa: BLE001
        st.example_untracked0 = set()


def _held(v: str, norm: str, loose: str) -> bool:
    a = _ASSIGN.match(v)
    if a:
        # env={"NAME": "value"}, setenv("NAME", "value"), NAME=value
        return bool(re.search(re.escape(a.group(1)) + r".{0,6}?" + re.escape(a.group(2)), norm))
    return _norm(v) in norm or _loose(v) in loose


def untested(st, expected_only: bool = False, unwritten: bool = False) -> list:
    """The request's examples no test of this turn holds; [] when the request
    asks for no tests, nothing was built, or it cannot be told. ``unwritten``:
    no test written yet means every example is still to test (the halfway
    check — click a9 had none at 15 minutes and the clock ended the turn
    before the finish gate)."""
    if not _on() or getattr(st, "readonly_mode", False) or getattr(st, "plan_mode", False) \
            or getattr(st, "builder", None) or getattr(st, "strict_finish", False) \
            or not getattr(st, "edits_made", 0):
        return []
    goal = str(getattr(st, "goal", "") or "")
    if not _ASKS_TESTS.search(goal) or _NO_TESTS.search(goal):
        return []
    wanted = examples(goal, expected_only=expected_only)
    said = " ".join(str(s) for s in (getattr(st, "steers", None) or []))
    wanted = [v for v in wanted if not said or _norm(v) not in _norm(said)]   # a steer names it
    wanted_pairs = [(a, b) for a, b in pairs(goal)       # a steer names it (long enough to tell)
                    if not said or len(_norm(b)) < 6 or _norm(b) not in _norm(said)]
    if not (wanted or wanted_pairs):
        return []
    from ._contract_gate import work_base
    base = work_base(st)
    if not base:
        return []                         # HEAD may hold the turn's own commit
    text = _changed_tests(st, str(getattr(st, "cwd", "") or "."), base)
    if text is None or (not text and not (unwritten and _ASKS_NEW_TESTS.search(goal))):
        return []                         # no test written yet, or git cannot tell
    if not text:                          # none yet: the expected results only
        wanted = [v for v in wanted if v in examples(goal, expected_only=True)]
    norm, loose = _norm(text), _loose(text)
    chunks = _windows(text)
    # a pair reads `input` -> `result` once the list wraps each item in backticks
    missed = [f"{a}` -> `{b}" for a, b in wanted_pairs if not _pair_held(a, b, chunks)]
    in_pairs = {_norm(b) for a, b in wanted_pairs}
    return missed + [v for v in wanted if not _held(v, norm, loose)
                     and (_norm(v) not in in_pairs or not missed)]


def message(missing: list) -> str:
    more = f"\n- … and {len(missing) - _SHOWN} more" if len(missing) > _SHOWN else ""
    return ("[automated example check — not the user] The request gives these exact "
            "results, and the tests you wrote this turn do not seem to contain them:\n"
            + "\n".join(f"- `{v}`" for v in missing[:_SHOWN]) + more + "\n"
            "Test each the way the request's example uses it: through the entry point "
            "it shows (the command, the environment variable, the function), with "
            "exactly that input (`a` -> `b`: one test calls with `a` and asserts `b`), "
            "asserting the result it gives. Run the tests and fix the code where they fail; do "
            "not change an expected value to match the code. If one of them is not a "
            "result the request expects, leave it out; do not write a test just to "
            "hold the string.")


def gate(st, step, builder=None, strict_finish=False):
    """Once per turn: ``"continue"`` with the list when expected results are
    untested."""
    if builder or strict_finish or getattr(st, "example_gate_sent", False):
        return None
    missing = untested(st, expected_only=True)
    if not missing:
        return None
    st.example_gate_sent = True
    if step.get("text"):
        yield {"type": "thought", "text": step["text"]}
    yield {"type": "thought", "role": "system",
           "text": f"▶ {len(missing)} example result(s) from the request have no test — continuing"}
    st.convo.append({"role": "user", "content": message(missing)})
    return "continue"


__all__ = ["examples", "gate", "message", "note_untracked", "untested"]
