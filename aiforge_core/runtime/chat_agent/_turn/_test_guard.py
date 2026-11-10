"""After an edit to a test file: did it change an assertion the repo already had?

marshmallow (unseen-repo bench, 11-10): the change added two parameters to
``Field.__repr__``; the existing ``test_repr`` failed; the model rewrote that
test's expected string to its new output, the suite went green and the turn
ended "existing tests pass" with an old test silently changed. The prompt
already says never weaken a test; this is the harness noticing it happen.

An assertion line present in the test file at the turn's base commit and gone
from it now is reported once: restore it and change the code, unless the user
asked for that behaviour to change. Only tracked test files are checked; new
tests are never reported. Off switch: ``AIFORGE_CHAT_TEST_GUARD=0``.

Before the answer, a send-back with the real diff (:func:`gate`) is OFF by
default (``AIFORGE_CHAT_TEST_GUARD_GATE=1``): on marshmallow it did get the
code fixed so the old test passed again, but cost 2 of 11 hidden tests and
2.5x the time in that run. What always happens: the answer's harness facts
name the test files whose old assertions changed (turn_facts_line).
Soft-fail throughout.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import textwrap
from collections import Counter

_MAX_SHOWN = 3
#: Test files by convention: a tests/ or spec/ folder, src/test/ (Maven,
#: Gradle), test_x.py, x_test.py / x_test.go, x.test.ts, x.spec.js,
#: XTest.java / XTests.cs / XSpec.kt, x_spec.rb, XTest.php.
_TEST_FILE = re.compile(
    r"(?:^|/)(?:tests?/|specs?/|__tests__/|src/test/)"
    r"|(?:^|/)test_[^/]*$"
    r"|_(?:test|spec)\.[a-z]+$"
    r"|\.(?:test|spec)\.[a-z]+$"
    r"|[A-Za-z0-9](?:Test|Tests|Spec|IT)\.(?:java|kt|scala|cs|php|swift|groovy)$")
#: One line that checks something, in the common frameworks: assert (Python,
#: C, Rust assert!/assert_eq!), unittest/JUnit/xUnit/minitest asserts,
#: pytest.raises(match=), Jest/Vitest/Chai/RSpec expect, Go t.Error/t.Fatal
#: and testify, Swift XCTAssert, PHP $this->assert.
_ASSERTION = re.compile(
    r"^\s*assert\w*\b|\bassert\w*!?\(|\bAssert(?:ions)?\.\w+\(|\bXCTAssert\w*\("
    r"|\bpytest\.raises\(|\bmatch\s*=|\bexpect\s*[({]|\.(?:to|not_to|to_not)\b"
    r"|\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\b|\b(?:require|assert)\.\w+\("
    r"|\bshould(?:\.|_)\w+|\$this->assert\w*\(|\bassertThat\(")


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_TEST_GUARD", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _git(cwd, *args) -> "str | None":
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", str(cwd), *args],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _norm(line: str) -> str:
    """A formatter's changes are not a change of what the test checks:
    spacing, quote style and a trailing comma are ignored."""
    s = " ".join(line.split()).replace("'", '"').rstrip(",;")
    s = re.sub(r"\s*([()\[\]{},])\s*", r"\1", s)
    return re.sub(r",([)\]}])", r"\1", s)


def _py_assertions(text: str) -> "Counter | None":
    """Whole assertion statements of a Python file, as ``ast.unparse`` writes
    them: a check split over lines (``assert repr(f) == (\n "..." \n)``) is
    one item, and quotes, spacing, commas and comments do not count."""
    try:
        tree = ast.parse(textwrap.dedent(text))
        text = textwrap.dedent(text)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    out: Counter = Counter()
    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                src = ast.unparse(item.context_expr)
                if _ASSERTION.search(src) or "raises" in src:
                    out["with " + src] += 1
        elif isinstance(node, ast.Assert):
            out[ast.unparse(node)] += 1
        elif isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign)):
            seg = ast.get_source_segment(text, node) or ""
            if _ASSERTION.search(seg.split("\n", 1)[0]):
                out[ast.unparse(node)] += 1
    return out


_STRINGS = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`[^`]*`')


def _statements(text: str):
    """Lines joined until their brackets close: one check written over
    several lines is one statement (Java, Go, JS, C#, Rust, Ruby...)."""
    buf, depth = [], 0
    for ln in text.splitlines():
        if not buf and ln.lstrip().startswith(("#", "//", "/*", "*")):
            continue
        buf.append(ln)
        bare = _STRINGS.sub("", ln)
        depth += sum(bare.count(c) for c in "([{") - sum(bare.count(c) for c in ")]}")
        if depth <= 0 or len(buf) > 40:
            yield "\n".join(buf)
            buf, depth = [], 0
    if buf:
        yield "\n".join(buf)


def _assertions(text: str) -> Counter:
    py = _py_assertions(text)
    if py is not None:
        return py
    return Counter(_norm(st) for st in _statements(text)
                   if _ASSERTION.search(st.split("\n", 1)[0]))


#: Turning a test off without touching its assertions.
_SKIP = re.compile(
    r"@pytest\.mark\.(?:skip|xfail)\b|\bpytest\.(?:skip|xfail)\(|@unittest\.skip"
    r"|\b(?:it|test|describe)\.(?:skip|todo)\(|\bx(?:it|describe|test)\("
    r"|\bt\.Skip(?:f|Now)?\(|@(?:Disabled|Ignore)\b|#\[ignore\]|\bskip\(\s*[\"']")


def lost_assertions(before: str, after: str) -> list[str]:
    """Assertion lines of ``before`` that ``after`` no longer has (by count),
    then skip markers ``after`` added."""
    gone = _assertions(before) - _assertions(after)
    skips = (Counter(_norm(ln) for ln in after.splitlines() if _SKIP.search(ln))
             - Counter(_norm(ln) for ln in before.splitlines() if _SKIP.search(ln)))
    return [*gone.elements(), *(f"(new skip) {ln}" for ln in skips.elements())]


_TESTS_WORD = r"(?:tests?\b|test_\w+|\w+_test\b|\w+Tests?\b|specs?\b)"
#: "do not change the test", "without touching the tests": never allowed.
_FORBIDS = re.compile(
    r"\b(?:do\s+not|don'?t|never|must\s+not|without|not\s+allowed\s+to)\s+(?:\w+\s+){0,2}"
    r"(?:chang|modif|edit|touch|alter|updat|weaken|delet|remov|rewrit)\w*\b[^.;\n]{0,40}"
    + _TESTS_WORD, re.I)
#: "update the tests", "update every use in `sqlglot/` and `tests/`",
#: "tests for the old module, which you may delete", "adjust test_x.py".
_ALLOWS = re.compile(
    r"\b(?:update|change|adjust|rewrite|replace|delete|remove|rename)\w*\b[^.;\n]{0,60}"
    + _TESTS_WORD +
    r"|\b" + _TESTS_WORD + r"[^.;\n]{0,80}\b(?:may|can|should|must|need\s+to)\s+(?:be\s+)?"
    r"(?:update|change|adjust|rewrit|replac|delet|remov)\w*"
    r"|\b(?:which|that)\s+you\s+may\s+(?:delete|remove|update|change)"
    r"|\btests?\s+to\s+(?:update|change|adjust|rewrite|fix)\b",
    re.I)


def allows_test_changes(goal: str, rel: str = "") -> bool:
    """The request says existing tests may change (a rewrite that replaces
    old behaviour, a rename, "update the tests") and nowhere says not to.
    Naming the failing test is not permission: a bug report names it too."""
    if not goal or _FORBIDS.search(goal):
        return False
    return bool(_ALLOWS.search(goal))


def asks_for_tests(goal: str) -> bool:
    """The request asks for tests (the example gate's own reading)."""
    from ._example_gate import _ASKS_TESTS, _NO_TESTS
    return bool(_ASKS_TESTS.search(goal or "")) and not _NO_TESTS.search(goal or "")


def unasked_new_tests(st, files, cwd=""):
    """A NEW test file in a turn whose request asks for no tests: one note.
    Owner, 11-10: "sometimes it writes test cases without me even asking".
    Updating a test the change touches (a rename) is not new and is fine."""
    if not _on() or getattr(st, "test_guard_unasked_said", False) or not files:
        return
    goal = str(getattr(st, "goal", "") or "")
    if not goal or asks_for_tests(goal):
        return
    try:
        from ._contract_gate import work_base
        base = work_base(st)
    except Exception:  # noqa: BLE001
        base = None
    top = (_git(cwd or getattr(st, "cwd", "") or ".", "rev-parse", "--show-toplevel") or "").strip()
    if not base or not top:
        return
    new = []
    for shown, full in files:
        try:
            rel = os.path.relpath(os.path.realpath(full), os.path.realpath(top))
        except ValueError:
            continue
        if rel.startswith("..") or not _TEST_FILE.search(rel.replace(os.sep, "/")):
            continue
        if _git(top, "cat-file", "-e", f"{base}:{rel}") is None:
            new.append(rel)
    if not new:
        return
    st.test_guard_unasked_said = True
    yield {"type": "thought", "role": "system",
           "text": f"⚠ {new[0]} is a new test file; the request did not ask for tests"}
    from ._notes import hold
    hold(st, "[harness — not the user] You created a new test file (" + ", ".join(new[:3])
             + ") but the request did not ask for tests. Keep to what was asked: remove "
             "it, check the change by running the existing tests and the request's own "
             "examples, and name the tests you would add under \"Suggested, not done\".")


_NEW_TEST = re.compile(r"\s*(?:async\s+)?(?:def\s+test|it\s*\(|test\s*\(|func\s+Test|@Test)")


def _tests_written(cwd, base) -> "bool | None":
    """The turn added a test since ``base``: a new test function/assertion in
    a tracked test file, or a new (untracked or added) test file. None: git
    cannot tell."""
    top = (_git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    if not top or not base:
        return None
    diff = _git(top, "diff", "-U0", base)
    if diff is None:
        return None
    current = ""
    for ln in diff.splitlines():
        if ln.startswith("+++ "):
            current = ln[6:] if ln.startswith("+++ b/") else ""
            continue
        if not (ln.startswith("+") and current and _TEST_FILE.search(current)):
            continue
        body = ln[1:]
        if _ASSERTION.search(body) or _NEW_TEST.match(body):
            return True
    others = _git(top, "ls-files", "--others", "--exclude-standard") or ""
    return any(_TEST_FILE.search(p) for p in others.splitlines())


def missing_tests_gate(st, step, builder=None, strict_finish=False):
    """Before the answer, once: the request asks for tests and the turn wrote
    none. sqlglot-trigger, 3 runs: "add your own tests ... plus fall-back
    checks in the MySQL / Oracle / SQLite / T-SQL dialect tests" -> no test
    written, the fall-back the tests would have caught broken (1/5 on that
    part; OpenCode wrote 14-17 test lines and got 5/5)."""
    # OFF by default (AIFORGE_CHAT_MISSING_TESTS_GATE=1 turns it on). Live:
    # sqlglot 15 -> 17/25, but attrs was 13/18 when it fired and the extra
    # round broke the import: 0/18. Extra work after a working answer needs
    # a way back before this can be on.
    if os.environ.get("AIFORGE_CHAT_MISSING_TESTS_GATE", "0").strip().lower() in (
            "0", "false", "no", "off", ""):
        return None
    if (builder or strict_finish or not _on() or getattr(st, "missing_tests_sent", False)
            or getattr(st, "edits_made", 0) <= 0
            or any(getattr(st, k, False) for k in ("readonly_mode", "plan_mode"))):
        return None
    goal = str(getattr(st, "goal", "") or "")
    if not asks_for_tests(goal):
        return None
    try:
        from ._contract_gate import work_base
        written = _tests_written(getattr(st, "cwd", None) or ".", work_base(st))
    except Exception:  # noqa: BLE001 — never block an answer on this
        return None
    if written is not False:
        return None
    st.missing_tests_sent = True
    if step.get("text"):
        yield {"type": "thought", "text": step["text"]}
    yield {"type": "thought", "role": "system",
           "text": "▶ the request asks for tests and none were written — continuing"}
    st.convo.append({"role": "user", "content":
        "[harness — not the user] Before you answer: the request asks for tests, and "
        "this turn has not added any. Add them now in the repo's test files: one per "
        "requirement, example and error case the request states, including every "
        "place where it says existing behaviour must stay as it is (those are the "
        "easiest to break). Run them and fix the code where they fail."})
    return "continue"


def check(st, name, files, args, cwd=""):
    """Events + a held note when the edit just landed changed or removed an
    assertion a tracked test file had at the turn's base."""
    if not _on() or not files:
        return
    try:
        from ._contract_gate import work_base
        base = work_base(st)
    except Exception:  # noqa: BLE001
        base = None
    if not base:
        return
    top = (_git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    if not top:
        return
    said = getattr(st, "test_guard_said", None)
    if said is None:
        said = st.test_guard_said = set()
    for shown, full in files:
        try:
            rel = os.path.relpath(os.path.realpath(full), os.path.realpath(top))
        except ValueError:
            continue
        if rel.startswith("..") or not _TEST_FILE.search(rel.replace(os.sep, "/")):
            continue
        before = _git(top, "show", f"{base}:{rel}")
        if before is None:
            continue                    # a new test file: nothing it had before
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                after = fh.read()
        except OSError:
            continue
        all_lost = lost_assertions(before, after)
        lost = [ln for ln in all_lost if (rel, ln) not in said]
        if not lost:
            # Still gone after the note, and this edit touched the file again
            # (marshmallow tg1a: the exact repr check became eight loose
            # `in` checks): one firmer reminder, then the finish gate.
            again = getattr(st, "test_guard_again", None)
            if again is None:
                again = st.test_guard_again = set()
            if all_lost and rel not in again and not allows_test_changes(
                    str(getattr(st, "goal", "") or ""), rel):
                again.add(rel)
                from ._notes import hold
                hold(st, "[harness — not the user] The old assertion in "
                         f"`{rel}` is still not back:\n    {all_lost[0][:200]}\n"
                         "Rewriting it as looser checks is the same as removing it. "
                         "Restore the original line exactly and change the code until "
                         "it passes.")
            continue
        said.update((rel, ln) for ln in lost)
        shown_lines = "\n".join(f"    {ln[:200]}" for ln in lost[:_MAX_SHOWN])
        more = f"\n    (+{len(lost) - _MAX_SHOWN} more)" if len(lost) > _MAX_SHOWN else ""
        yield {"type": "thought", "role": "system",
               "text": f"⚠ edit to {shown} changed an existing assertion: {lost[0][:120]}"}
        from ._notes import hold
        head = ("[harness — not the user] This edit changed or removed an assertion "
                f"that `{rel}` already had before this request:\n{shown_lines}{more}\n")
        if allows_test_changes(str(getattr(st, "goal", "") or ""), rel):
            hold(st, head + "The request allows some existing tests to change. Keep "
                     "this only if it is one of those; otherwise put it back and change "
                     "the code so the old test passes.")
        else:
            hold(st, head + "An existing test that fails after your change means the "
                     "change broke it. Put the assertion back (`git diff` shows the old "
                     "line) and change your code so the old test passes. Only keep the "
                     "new assertion if the user asked for that existing behaviour to "
                     "change.")


def still_lost(st) -> list:
    """(rel, line) for every assertion a tracked test file had at the turn's
    base that it no longer has."""
    try:
        from ._contract_gate import work_base
        base = work_base(st)
    except Exception:  # noqa: BLE001
        base = None
    return lost_in(getattr(st, "cwd", None), base)


def lost_in(cwd, base) -> list:
    """(rel, line): assertions the test files had at ``base`` and lack now."""
    if not cwd or not base:
        return []
    top = (_git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    changed = _git(top, "diff", "--name-only", base) if top else None
    out = []
    for rel in (changed or "").splitlines():
        if not _TEST_FILE.search(rel):
            continue
        before = _git(top, "show", f"{base}:{rel}")
        full = os.path.join(top, rel)
        if before is None:
            continue
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                after = fh.read()
        except OSError:
            after = ""                  # the whole file deleted
        out += [(rel, ln) for ln in lost_assertions(before, after)]
    return out


def _hunks(top, base, rels, cap=40) -> str:
    """The real old and new lines (``git diff``), not a normalised form: a
    model told to "restore" a line it cannot see rewrote it again."""
    out = []
    for rel in dict.fromkeys(rels):
        diff = _git(top, "diff", "-U2", base, "--", rel) or ""
        body = [ln for ln in diff.splitlines()
                if not ln.startswith(("diff ", "index ", "--- ", "+++ "))]
        out.append(f"{rel}:\n" + "\n".join(body[:cap])
                   + ("\n..." if len(body) > cap else ""))
    return "\n".join(out)


_GATE_MAX = 2


def gate(st, step, builder=None, strict_finish=False):
    """Before the answer: an existing assertion still changed or removed, in
    a request that did not allow test changes -> continue with the real diff.
    Twice at most, the second only after more edits."""
    if os.environ.get("AIFORGE_CHAT_TEST_GUARD_GATE", "0").strip().lower() in (
            "0", "false", "no", "off", ""):
        return None                     # off by default: see the module note
    sent = getattr(st, "test_guard_gate_sent", 0) or 0
    edits = getattr(st, "edits_made", 0)
    if (builder or not _on() or edits <= 0 or sent >= _GATE_MAX
            or (sent and edits == getattr(st, "test_guard_gate_edits", -1))
            or allows_test_changes(str(getattr(st, "goal", "") or ""))):
        return None
    try:
        lost = still_lost(st)
    except Exception:  # noqa: BLE001 — never block an answer on this
        return None
    if not lost:
        return None
    st.test_guard_gate_sent = sent + 1
    st.test_guard_gate_edits = edits
    if step.get("text"):
        yield {"type": "thought", "text": step["text"]}
    yield {"type": "thought", "role": "system",
           "text": f"▶ {len(lost)} existing test assertion(s) changed — continuing"}
    try:
        from ._contract_gate import work_base
        top = (_git(st.cwd, "rev-parse", "--show-toplevel") or "").strip()
        diff = _hunks(top, work_base(st), [rel for rel, _ in lost])
    except Exception:  # noqa: BLE001
        diff = "\n".join(f"    {rel}: {ln[:180]}" for rel, ln in lost[:_MAX_SHOWN])
    again = " (second time: the earlier restore did not bring them back)" if sent else ""
    st.convo.append({"role": "user", "content":
        f"[harness — not the user] Before you answer{again}: existing tests were "
        "changed, and the request did not ask for that. This is the change "
        "(`-` = the line as it was before this request):\n"
        f"```diff\n{diff}\n```\n"
        "Put every `-` line back exactly as shown (file_patch with the `+` lines as "
        "old text). Then the old test fails again: change the CODE you wrote "
        "(not the test) so it passes, keeping what the request asked for. If the "
        "request cannot be done without changing that test, leave the test as it "
        "was and say so in the answer."})
    return "continue"


__all__ = ["allows_test_changes", "asks_for_tests", "missing_tests_gate", "check", "gate", "lost_assertions",
           "lost_in", "still_lost", "unasked_new_tests"]
