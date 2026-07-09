#!/usr/bin/env python3
"""Multi-language coding+reasoning duel — OUR generation seam vs DSPy.

Supersedes dspy_java_duel.py with two harness fixes found by dissecting the
first run, plus Python and C++ arms:
  1. CRASH-PROOF SCORING — every assertion is individually guarded, so an
     exception in one case no longer zeroes a run that passed others (the
     Java evaluator scored 0/0 while actually ~7/15).
  2. dspy runs with the explicit ChatAdapter — its JSONAdapter fallback
     sends response_format that LM Studio 400-rejects (cost dspy a whole
     task in run 1). Configuring the working adapter is the fair way to
     run dspy against this server.

Both arms share the IDENTICAL 3-round error-feedback loop; the ONLY
variable is the generation call (our doer-style prompt vs
dspy.ChainOfThought). Two tough tasks per language (expression evaluator;
TTL-LRU cache), scored by fixed native test files:
  java   → javac/java     python → python3     cpp → g++ -std=c++17

Run:
    uv run --with dspy python scripts/dspy_code_duel.py \
        --base-url http://127.0.0.1:1234/v1 --model <id> [--langs java,python,cpp]

NOTE: no `from __future__ import annotations` (dspy rejects stringified
hints).
"""
import argparse
import re
import subprocess
import tempfile
from pathlib import Path

ROUNDS = 3

_EVAL_RULES = (
    "Full arithmetic expression evaluation: + - * / with correct precedence, "
    "nested parentheses, unary minus (also after '(' and operators, e.g. "
    "2*-3, -(1+2), --4), variable names (letters/digits/underscores, not "
    "starting with a digit) resolved from the provided mapping, whitespace "
    "tolerated anywhere, integer and decimal literals, division is floating "
    "division. Unknown variable or malformed input must raise/throw the "
    "specified error type.")

_LRU_RULES = (
    "LRU cache with TTL. Semantics: eviction of the least-recently-used "
    "entry when OVER capacity on insert of a NEW key (updates never evict); "
    "get refreshes recency; an entry is expired when now - lastWriteTime >= "
    "ttl (expired entries are absent for get/size, don't count toward "
    "capacity, pruned lazily); put of an existing key resets its TTL; time "
    "is always passed in explicitly — never read system time.")

TASKS = {
    # ── JAVA ─────────────────────────────────────────────────────────
    "java": [
        {
            "name": "evaluator", "file": "Evaluator.java",
            "spec": ("Implement public class Evaluator (no package) with:\n"
                     "  public static double eval(String expr, java.util.Map<String, Double> vars)\n"
                     + _EVAL_RULES + " Error type: IllegalArgumentException."),
            "test_file": "Test.java",
            "test": r"""
import java.util.*;
import java.util.function.Supplier;
public class Test {
    static int passed = 0, total = 0;
    static void eq(Supplier<Double> s, double want, String name) {
        total++;
        try {
            double got = s.get();
            if (Math.abs(got - want) < 1e-9) passed++;
            else System.out.println("FAIL " + name + " got=" + got + " want=" + want);
        } catch (Throwable t) { System.out.println("FAIL " + name + " threw " + t); }
    }
    static void err(Supplier<Double> s, String name) {
        total++;
        try { s.get(); System.out.println("FAIL " + name + " (no throw)"); }
        catch (IllegalArgumentException e) { passed++; }
        catch (Throwable t) { System.out.println("FAIL " + name + " wrong ex " + t); }
    }
    public static void main(String[] a) {
        Map<String, Double> v = new HashMap<>();
        v.put("x", 4.0); v.put("y", 0.5); v.put("long_name", 10.0);
        eq(() -> Evaluator.eval("1+2*3", v), 7.0, "precedence");
        eq(() -> Evaluator.eval("(1+2)*3", v), 9.0, "parens");
        eq(() -> Evaluator.eval("2*-3", v), -6.0, "unary-after-op");
        eq(() -> Evaluator.eval("-(1+2)", v), -3.0, "unary-paren");
        eq(() -> Evaluator.eval("--4", v), 4.0, "double-unary");
        eq(() -> Evaluator.eval("7/2", v), 3.5, "double-div");
        eq(() -> Evaluator.eval("x*y", v), 2.0, "vars");
        eq(() -> Evaluator.eval("long_name + x", v), 14.0, "underscore var");
        eq(() -> Evaluator.eval(" 1 + 2 ", v), 3.0, "whitespace");
        eq(() -> Evaluator.eval("2*(3+(4-1))/4", v), 3.0, "nested");
        eq(() -> Evaluator.eval("1.5*2", v), 3.0, "decimal");
        eq(() -> Evaluator.eval("10-3-2", v), 5.0, "left-assoc");
        err(() -> Evaluator.eval("z+1", v), "unknown var");
        err(() -> Evaluator.eval("1+*2", v), "malformed");
        err(() -> Evaluator.eval("(1+2", v), "unbalanced");
        System.out.println("PASSED=" + passed + " TOTAL=" + total);
    }
}
""",
        },
        {
            "name": "ttl-lru", "file": "TtlLruCache.java",
            "spec": ("Implement public class TtlLruCache (no package) with:\n"
                     "  public TtlLruCache(int capacity, long ttlMillis)\n"
                     "  public void put(String key, String value, long nowMillis)\n"
                     "  public String get(String key, long nowMillis)  // null if absent/expired\n"
                     "  public int size(long nowMillis)\n" + _LRU_RULES),
            "test_file": "Test.java",
            "test": r"""
public class Test {
    static int passed = 0, total = 0;
    static void eq(Object got, Object want, String name) {
        total++;
        if (got == null ? want == null : got.equals(want)) passed++;
        else System.out.println("FAIL " + name + " got=" + got + " want=" + want);
    }
    static void step(Runnable r, String name) {
        try { r.run(); } catch (Throwable t) { System.out.println("STEP-FAIL " + name + " " + t); }
    }
    public static void main(String[] a) {
        TtlLruCache c = new TtlLruCache(2, 100);
        step(() -> { c.put("a", "1", 0); c.put("b", "2", 10); }, "seed");
        step(() -> eq(c.get("a", 20), "1", "basic get"), "t1");
        step(() -> c.put("c", "3", 30), "insert c");
        step(() -> eq(c.get("b", 31), null, "lru evicted b"), "t2");
        step(() -> eq(c.get("a", 32), "1", "a survived"), "t3");
        step(() -> eq(c.get("c", 33), "3", "c present"), "t4");
        step(() -> eq(c.get("a", 125), null, "a expired"), "t5");
        step(() -> c.put("d", "4", 130), "insert d");
        step(() -> eq(c.get("c", 131), null, "c gone or live? c wrote 30, 131-30>=100 -> expired"), "t6");
        step(() -> eq(c.size(135), 1, "size live only"), "t7");
        step(() -> c.put("d", "5", 140), "update d");
        step(() -> eq(c.get("d", 235), "5", "ttl reset on update"), "t8");
        TtlLruCache one = new TtlLruCache(1, 1000);
        step(() -> { one.put("x", "1", 0); one.put("y", "2", 1); }, "cap1");
        step(() -> eq(one.get("x", 2), null, "cap1 evicts"), "t9");
        step(() -> eq(one.get("y", 3), "2", "newest kept"), "t10");
        System.out.println("PASSED=" + passed + " TOTAL=" + total);
    }
}
""",
        },
    ],
    # ── PYTHON ───────────────────────────────────────────────────────
    "python": [
        {
            "name": "evaluator", "file": "solution.py",
            "spec": ("Implement in a module (solution.py):\n"
                     "  def eval_expr(expr: str, variables: dict[str, float]) -> float\n"
                     + _EVAL_RULES + " Error type: ValueError. Do NOT use "
                     "python eval()/ast."),
            "test_file": "test_run.py",
            "test": r"""
import solution
passed = total = 0
def eq(fn, want, name):
    global passed, total
    total += 1
    try:
        got = fn()
        if abs(got - want) < 1e-9: passed += 1
        else: print(f"FAIL {name} got={got} want={want}")
    except Exception as e: print(f"FAIL {name} threw {e!r}")
def err(fn, name):
    global passed, total
    total += 1
    try:
        fn(); print(f"FAIL {name} (no raise)")
    except ValueError: passed += 1
    except Exception as e: print(f"FAIL {name} wrong ex {e!r}")
v = {"x": 4.0, "y": 0.5, "long_name": 10.0}
eq(lambda: solution.eval_expr("1+2*3", v), 7.0, "precedence")
eq(lambda: solution.eval_expr("(1+2)*3", v), 9.0, "parens")
eq(lambda: solution.eval_expr("2*-3", v), -6.0, "unary-after-op")
eq(lambda: solution.eval_expr("-(1+2)", v), -3.0, "unary-paren")
eq(lambda: solution.eval_expr("--4", v), 4.0, "double-unary")
eq(lambda: solution.eval_expr("7/2", v), 3.5, "div")
eq(lambda: solution.eval_expr("x*y", v), 2.0, "vars")
eq(lambda: solution.eval_expr("long_name + x", v), 14.0, "underscore var")
eq(lambda: solution.eval_expr(" 1 + 2 ", v), 3.0, "whitespace")
eq(lambda: solution.eval_expr("2*(3+(4-1))/4", v), 3.0, "nested")
eq(lambda: solution.eval_expr("1.5*2", v), 3.0, "decimal")
eq(lambda: solution.eval_expr("10-3-2", v), 5.0, "left-assoc")
err(lambda: solution.eval_expr("z+1", v), "unknown var")
err(lambda: solution.eval_expr("1+*2", v), "malformed")
err(lambda: solution.eval_expr("(1+2", v), "unbalanced")
print(f"PASSED={passed} TOTAL={total}")
""",
        },
        {
            "name": "ttl-lru", "file": "solution.py",
            "spec": ("Implement in a module (solution.py):\n"
                     "  class TtlLruCache:\n"
                     "      def __init__(self, capacity: int, ttl_millis: int)\n"
                     "      def put(self, key: str, value: str, now_millis: int) -> None\n"
                     "      def get(self, key: str, now_millis: int) -> str | None\n"
                     "      def size(self, now_millis: int) -> int\n" + _LRU_RULES),
            "test_file": "test_run.py",
            "test": r"""
from solution import TtlLruCache
passed = total = 0
def eq(fn, want, name):
    global passed, total
    total += 1
    try:
        got = fn()
        if got == want: passed += 1
        else: print(f"FAIL {name} got={got} want={want}")
    except Exception as e: print(f"FAIL {name} threw {e!r}")
c = TtlLruCache(2, 100)
c.put("a", "1", 0); c.put("b", "2", 10)
eq(lambda: c.get("a", 20), "1", "basic get")
c.put("c", "3", 30)
eq(lambda: c.get("b", 31), None, "lru evicted b")
eq(lambda: c.get("a", 32), "1", "a survived")
eq(lambda: c.get("c", 33), "3", "c present")
eq(lambda: c.get("a", 125), None, "a expired")
c.put("d", "4", 130)
eq(lambda: c.get("c", 131), None, "c expired (wrote 30)")
eq(lambda: c.size(135), 1, "size live only")
c.put("d", "5", 140)
eq(lambda: c.get("d", 235), "5", "ttl reset on update")
one = TtlLruCache(1, 1000)
one.put("x", "1", 0); one.put("y", "2", 1)
eq(lambda: one.get("x", 2), None, "cap1 evicts")
eq(lambda: one.get("y", 3), "2", "newest kept")
print(f"PASSED={passed} TOTAL={total}")
""",
        },
    ],
    # ── C++ ──────────────────────────────────────────────────────────
    "cpp": [
        {
            "name": "evaluator", "file": "solution.cpp",
            "spec": ("Implement in solution.cpp (will be #included by the "
                     "test — provide ONLY the implementation, no main):\n"
                     "  double eval_expr(const std::string& expr, const std::map<std::string, double>& vars)\n"
                     + _EVAL_RULES + " Error type: throw std::invalid_argument. "
                     "Include the headers you need."),
            "test_file": "test.cpp",
            "test": r"""
#include <bits/stdc++.h>
#include "solution.cpp"
int passed = 0, total = 0;
void eq(std::function<double()> f, double want, const std::string& name) {
    total++;
    try {
        double got = f();
        if (std::fabs(got - want) < 1e-9) passed++;
        else std::cout << "FAIL " << name << " got=" << got << " want=" << want << "\n";
    } catch (const std::exception& e) { std::cout << "FAIL " << name << " threw " << e.what() << "\n"; }
}
void err(std::function<double()> f, const std::string& name) {
    total++;
    try { f(); std::cout << "FAIL " << name << " (no throw)\n"; }
    catch (const std::invalid_argument&) { passed++; }
    catch (const std::exception& e) { std::cout << "FAIL " << name << " wrong ex " << e.what() << "\n"; }
}
int main() {
    std::map<std::string, double> v{{"x", 4.0}, {"y", 0.5}, {"long_name", 10.0}};
    eq([&]{ return eval_expr("1+2*3", v); }, 7.0, "precedence");
    eq([&]{ return eval_expr("(1+2)*3", v); }, 9.0, "parens");
    eq([&]{ return eval_expr("2*-3", v); }, -6.0, "unary-after-op");
    eq([&]{ return eval_expr("-(1+2)", v); }, -3.0, "unary-paren");
    eq([&]{ return eval_expr("--4", v); }, 4.0, "double-unary");
    eq([&]{ return eval_expr("7/2", v); }, 3.5, "div");
    eq([&]{ return eval_expr("x*y", v); }, 2.0, "vars");
    eq([&]{ return eval_expr("long_name + x", v); }, 14.0, "underscore var");
    eq([&]{ return eval_expr(" 1 + 2 ", v); }, 3.0, "whitespace");
    eq([&]{ return eval_expr("2*(3+(4-1))/4", v); }, 3.0, "nested");
    eq([&]{ return eval_expr("1.5*2", v); }, 3.0, "decimal");
    eq([&]{ return eval_expr("10-3-2", v); }, 5.0, "left-assoc");
    err([&]{ return eval_expr("z+1", v); }, "unknown var");
    err([&]{ return eval_expr("1+*2", v); }, "malformed");
    err([&]{ return eval_expr("(1+2", v); }, "unbalanced");
    std::cout << "PASSED=" << passed << " TOTAL=" << total << "\n";
}
""",
        },
        {
            "name": "ttl-lru", "file": "solution.cpp",
            "spec": ("Implement in solution.cpp (will be #included by the "
                     "test — ONLY the implementation, no main):\n"
                     "  class TtlLruCache {\n"
                     "   public:\n"
                     "    TtlLruCache(int capacity, long long ttl_millis);\n"
                     "    void put(const std::string& key, const std::string& value, long long now_millis);\n"
                     "    std::optional<std::string> get(const std::string& key, long long now_millis);\n"
                     "    int size(long long now_millis);\n  };\n" + _LRU_RULES
                     + " Include the headers you need."),
            "test_file": "test.cpp",
            "test": r"""
#include <bits/stdc++.h>
#include "solution.cpp"
int passed = 0, total = 0;
void eq(std::optional<std::string> got, std::optional<std::string> want, const std::string& name) {
    total++;
    if (got == want) passed++;
    else std::cout << "FAIL " << name << "\n";
}
void eqi(int got, int want, const std::string& name) {
    total++;
    if (got == want) passed++;
    else std::cout << "FAIL " << name << " got=" << got << " want=" << want << "\n";
}
int main() {
    TtlLruCache c(2, 100);
    c.put("a", "1", 0); c.put("b", "2", 10);
    eq(c.get("a", 20), std::optional<std::string>("1"), "basic get");
    c.put("c", "3", 30);
    eq(c.get("b", 31), std::nullopt, "lru evicted b");
    eq(c.get("a", 32), std::optional<std::string>("1"), "a survived");
    eq(c.get("c", 33), std::optional<std::string>("3"), "c present");
    eq(c.get("a", 125), std::nullopt, "a expired");
    c.put("d", "4", 130);
    eq(c.get("c", 131), std::nullopt, "c expired");
    eqi(c.size(135), 1, "size live only");
    c.put("d", "5", 140);
    eq(c.get("d", 235), std::optional<std::string>("5"), "ttl reset");
    TtlLruCache one(1, 1000);
    one.put("x", "1", 0); one.put("y", "2", 1);
    eq(one.get("x", 2), std::nullopt, "cap1 evicts");
    eq(one.get("y", 3), std::optional<std::string>("2"), "newest kept");
    std::cout << "PASSED=" << passed << " TOTAL=" << total << "\n";
}
""",
        },
    ],
}

_FENCE = {"java": "java", "python": "python", "cpp": "cpp"}


def _extract(text: str, lang: str) -> str:
    blocks = re.findall(r"```(?:\w+)?\s*\n(.*?)```", text or "", re.DOTALL)
    if blocks:
        return max(blocks, key=len)      # biggest block = the file (not a snippet)
    return text or ""


def _run_case(lang: str, task: dict, src: str) -> tuple[bool, int, int, str]:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / task["file"]).write_text(src, encoding="utf-8")
        (d / task["test_file"]).write_text(task["test"], encoding="utf-8")
        try:
            if lang == "java":
                c = subprocess.run(["javac", task["file"], task["test_file"]],
                                   cwd=td, capture_output=True, text=True, timeout=90)
                if c.returncode != 0:
                    return False, 0, 0, (c.stderr or c.stdout)[-2500:]
                r = subprocess.run(["java", "-cp", ".", "Test"], cwd=td,
                                   capture_output=True, text=True, timeout=30)
            elif lang == "python":
                r = subprocess.run(["python3", task["test_file"]], cwd=td,
                                   capture_output=True, text=True, timeout=30)
                if r.returncode != 0 and "PASSED=" not in (r.stdout or ""):
                    return False, 0, 0, (r.stderr or r.stdout)[-2500:]
            else:  # cpp
                c = subprocess.run(["g++", "-std=c++17", "-O0", task["test_file"],
                                    "-o", "test_bin"], cwd=td,
                                   capture_output=True, text=True, timeout=120)
                if c.returncode != 0:
                    return False, 0, 0, (c.stderr or c.stdout)[-2500:]
                r = subprocess.run(["./test_bin"], cwd=td, capture_output=True,
                                   text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return False, 0, 0, "TIMEOUT (compile or run hung)"
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"PASSED=(\d+) TOTAL=(\d+)", out)
        if not m:
            return True, 0, 0, out[-2500:]
        return True, int(m.group(1)), int(m.group(2)), out[-2500:]


_OUR_SYS = (
    "You are a senior {lang} engineer. Produce COMPLETE, COMPILING {lang} "
    "source for the requested file — production quality, no external "
    "dependencies. Think through edge cases FIRST in prose, then output the "
    "full file as the LAST fenced code block, nothing after it.")


def _run_arm(gen, lang: str, task: dict) -> tuple[int, int, int]:
    feedback = ""
    best = (0, 0)
    for rnd in range(1, ROUNDS + 1):
        src = _extract(gen(lang, task, feedback), lang)
        ok, p, t, out = _run_case(lang, task, src)
        if ok and t and p == t:
            return rnd, p, t
        if (p, t) > best:
            best = (p, t)
        feedback = (("COMPILE ERROR:\n" if not ok else
                     "TEST OUTPUT (fix every FAIL):\n") + out
                    + "\n\nYOUR PREVIOUS SOURCE:\n" + src[:6000])
    return ROUNDS, best[0], best[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="not-needed")
    ap.add_argument("--langs", default="java,python,cpp")
    args = ap.parse_args()
    langs = [x.strip() for x in args.langs.split(",") if x.strip() in TASKS]

    import httpx

    def ours_gen(lang: str, task: dict, feedback: str) -> str:
        user = (f"TASK:\n{task['spec']}\n\nYour file must pass this exact "
                f"test file ({task['test_file']}):\n```\n{task['test']}\n```\n"
                + (f"\nPREVIOUS ATTEMPT FEEDBACK:\n{feedback}\n" if feedback else ""))
        r = httpx.post(f"{args.base_url.rstrip('/')}/chat/completions",
                       json={"model": args.model, "temperature": 0,
                             "max_tokens": 8000,
                             "messages": [
                                 {"role": "system",
                                  "content": _OUR_SYS.format(lang=lang)},
                                 {"role": "user", "content": user}]},
                       headers={"Authorization": f"Bearer {args.api_key}"},
                       timeout=900)
        return r.json()["choices"][0]["message"]["content"]

    import dspy
    # Explicit ChatAdapter: the default JSONAdapter FALLBACK sends
    # response_format which LM Studio 400-rejects (cost dspy a task in run 1).
    dspy.configure(
        lm=dspy.LM(f"openai/{args.model}", api_base=args.base_url,
                   api_key=args.api_key, temperature=0, max_tokens=8000,
                   timeout=900),
        adapter=dspy.ChatAdapter())

    class WriteCode(dspy.Signature):
        """Write COMPLETE, COMPILING source for the requested file (no
        external deps) that passes the given test file. Reason about edge
        cases first."""
        language: str = dspy.InputField()
        spec: str = dspy.InputField()
        test_file: str = dspy.InputField()
        feedback: str = dspy.InputField(desc="errors from the previous attempt, 'none' on first try")
        source: str = dspy.OutputField(desc="the full source file content")

    dspy_mod = dspy.ChainOfThought(WriteCode)

    def dspy_gen(lang: str, task: dict, feedback: str) -> str:
        return dspy_mod(language=lang, spec=task["spec"],
                        test_file=task["test"],
                        feedback=feedback or "none").source

    print(f"{'lang':<8} {'task':<10} {'arm':<5} rounds  score")
    grand = {"ours": [0, 0], "dspy": [0, 0]}
    for lang in langs:
        for task in TASKS[lang]:
            for arm, gen in (("ours", ours_gen), ("dspy", dspy_gen)):
                try:
                    rnd, p, t = _run_arm(gen, lang, task)
                except Exception as exc:  # noqa: BLE001
                    print(f"{lang:<8} {task['name']:<10} {arm:<5} ERROR {str(exc)[:80]}")
                    continue
                grand[arm][0] += p
                grand[arm][1] += t or 1
                print(f"{lang:<8} {task['name']:<10} {arm:<5} {rnd:>6}  {p}/{t}",
                      flush=True)
    print(f"\nGRAND TOTAL  ours: {grand['ours'][0]}/{grand['ours'][1]}   "
          f"dspy: {grand['dspy'][0]}/{grand['dspy'][1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
