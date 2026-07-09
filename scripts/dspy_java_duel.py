#!/usr/bin/env python3
"""JAVA coding+reasoning duel — OUR generation seam vs DSPy, hardest-case.

Java is the local model's known ceiling. Both arms run the IDENTICAL
iterative harness (up to 3 rounds: generate → javac → run fixed tests →
feed raw errors back); the ONLY variable is the generation call:
  OURS — our production-style doer prompt via a raw chat completion
         (the same shape our reconcile fix-loop uses: raw error + source).
  DSPY — dspy.ChainOfThought over a typed Signature with the same inputs.

Three tasks, each judged by a FIXED pre-written test file (no deps, plain
javac/java): expression evaluator (precedence, parens, unary minus,
variables), LRU cache with TTL (strict eviction-order semantics),
topological sort with cycle extraction. Score = compile + assertions.

Run on a box with JDK:
    uv run --with dspy python scripts/dspy_java_duel.py \
        --base-url http://127.0.0.1:1234/v1 --model <id>

NOTE: no `from __future__ import annotations` — dspy rejects stringified
type hints.
"""
import argparse
import re
import subprocess
import tempfile
from pathlib import Path

TASKS = [
    {
        "name": "evaluator",
        "cls": "Evaluator",
        "spec": (
            "Implement public class Evaluator with:\n"
            "  public static double eval(String expr, java.util.Map<String, Double> vars)\n"
            "Full arithmetic expression evaluation: + - * / with correct "
            "precedence, parentheses (nested), unary minus (also after '(' "
            "and operators, e.g. 2*-3, -(1+2)), variable names resolved from "
            "vars (throw IllegalArgumentException for unknown variables), "
            "whitespace tolerated anywhere. Integer and decimal literals. "
            "Division is double division. Throw IllegalArgumentException on "
            "malformed input."),
        "test": r"""
import java.util.*;
public class Test {
    static int passed = 0, total = 0;
    static void eq(double got, double want, String name) {
        total++;
        if (Math.abs(got - want) < 1e-9) passed++;
        else System.out.println("FAIL " + name + " got=" + got + " want=" + want);
    }
    static void throwsIAE(Runnable r, String name) {
        total++;
        try { r.run(); System.out.println("FAIL " + name + " (no throw)"); }
        catch (IllegalArgumentException e) { passed++; }
        catch (Throwable t) { System.out.println("FAIL " + name + " wrong ex " + t); }
    }
    public static void main(String[] a) {
        Map<String, Double> v = new HashMap<>();
        v.put("x", 4.0); v.put("y", 0.5); v.put("long_name", 10.0);
        eq(Evaluator.eval("1+2*3", v), 7.0, "precedence");
        eq(Evaluator.eval("(1+2)*3", v), 9.0, "parens");
        eq(Evaluator.eval("2*-3", v), -6.0, "unary-after-op");
        eq(Evaluator.eval("-(1+2)", v), -3.0, "unary-before-paren");
        eq(Evaluator.eval("--4", v), 4.0, "double-unary");
        eq(Evaluator.eval("7/2", v), 3.5, "double-div");
        eq(Evaluator.eval("x*y", v), 2.0, "vars");
        eq(Evaluator.eval("long_name + x", v), 14.0, "long var");
        eq(Evaluator.eval(" 1 + 2 ", v), 3.0, "whitespace");
        eq(Evaluator.eval("2*(3+(4-1))/4", v), 3.0, "nested");
        eq(Evaluator.eval("1.5*2", v), 3.0, "decimal");
        eq(Evaluator.eval("10-3-2", v), 5.0, "left-assoc");
        throwsIAE(() -> Evaluator.eval("z+1", v), "unknown var");
        throwsIAE(() -> Evaluator.eval("1+*2", v), "malformed");
        throwsIAE(() -> Evaluator.eval("(1+2", v), "unbalanced");
        System.out.println("PASSED=" + passed + " TOTAL=" + total);
    }
}
""",
    },
    {
        "name": "ttl-lru",
        "cls": "TtlLruCache",
        "spec": (
            "Implement public class TtlLruCache with:\n"
            "  public TtlLruCache(int capacity, long ttlMillis)\n"
            "  public void put(String key, String value, long nowMillis)\n"
            "  public String get(String key, long nowMillis)  // null if absent/expired\n"
            "  public int size(long nowMillis)                // live entries only\n"
            "Semantics: LRU eviction when OVER capacity on put of a NEW key "
            "(updates of an existing key do not evict). get refreshes "
            "recency. An entry is expired when nowMillis - lastWriteTime >= "
            "ttlMillis (get/size must treat expired entries as absent; "
            "expired entries do not count toward capacity and are pruned "
            "lazily). put of an existing key resets its TTL. Time is passed "
            "in explicitly — never use System time. O(1) get/put expected "
            "(LinkedHashMap acceptable)."),
        "test": r"""
public class Test {
    static int passed = 0, total = 0;
    static void eq(Object got, Object want, String name) {
        total++;
        if (got == null ? want == null : got.equals(want)) passed++;
        else System.out.println("FAIL " + name + " got=" + got + " want=" + want);
    }
    public static void main(String[] a) {
        TtlLruCache c = new TtlLruCache(2, 100);
        c.put("a", "1", 0); c.put("b", "2", 10);
        eq(c.get("a", 20), "1", "basic get");
        c.put("c", "3", 30);                       // a was refreshed at 20 → b evicted
        eq(c.get("b", 31), null, "lru evicted b");
        eq(c.get("a", 32), "1", "a survived");
        eq(c.get("c", 33), "3", "c present");
        eq(c.get("a", 125), null, "a expired (write 0? refreshed? ttl from write)");
        c.put("d", "4", 130);
        eq(c.get("c", 131), "3", "c still live before its expiry");
        eq(c.get("c", 200), null, "c expired at 30+100<=200");
        eq(c.size(205), 1, "size counts live only (d)");
        c.put("d", "5", 210);                      // update resets ttl, no evict
        eq(c.get("d", 305), "5", "ttl reset on update");
        TtlLruCache one = new TtlLruCache(1, 1000);
        one.put("x", "1", 0); one.put("y", "2", 1);
        eq(one.get("x", 2), null, "capacity 1 evicts");
        eq(one.get("y", 3), "2", "newest kept");
        System.out.println("PASSED=" + passed + " TOTAL=" + total);
    }
}
""",
    },
    {
        "name": "toposort",
        "cls": "TopoSort",
        "spec": (
            "Implement public class TopoSort with:\n"
            "  public static java.util.List<String> sort(java.util.Map<String, java.util.List<String>> deps)\n"
            "deps maps node -> list of nodes it DEPENDS ON (must come "
            "earlier). Return a valid topological order containing every "
            "node mentioned anywhere (keys and dependency values). "
            "DETERMINISM: whenever multiple nodes are simultaneously "
            "available, pick the LEXICOGRAPHICALLY SMALLEST next. On a "
            "cycle, throw IllegalStateException whose message contains "
            "the cycle's nodes joined by '->' starting from its "
            "lexicographically smallest member (e.g. \"a->b->c->a\")."),
        "test": r"""
import java.util.*;
public class Test {
    static int passed = 0, total = 0;
    static void eq(Object got, Object want, String name) {
        total++;
        if (got.equals(want)) passed++;
        else System.out.println("FAIL " + name + " got=" + got + " want=" + want);
    }
    public static void main(String[] a) {
        Map<String, List<String>> d = new HashMap<>();
        d.put("b", List.of("a")); d.put("c", List.of("a")); d.put("d", List.of("b", "c"));
        eq(TopoSort.sort(d), List.of("a", "b", "c", "d"), "diamond lexi");
        Map<String, List<String>> e = new HashMap<>();
        e.put("z", List.of()); e.put("m", List.of()); e.put("a", List.of());
        eq(TopoSort.sort(e), List.of("a", "m", "z"), "all-free lexi");
        Map<String, List<String>> f = new HashMap<>();
        f.put("app", List.of("lib", "core")); f.put("lib", List.of("core"));
        eq(TopoSort.sort(f), List.of("core", "lib", "app"), "implicit node core");
        Map<String, List<String>> g = new HashMap<>();
        g.put("a", List.of("c")); g.put("b", List.of("a")); g.put("c", List.of("b"));
        total++;
        try { TopoSort.sort(g); System.out.println("FAIL cycle (no throw)"); }
        catch (IllegalStateException ex) {
            if (ex.getMessage() != null && ex.getMessage().contains("a->c->b->a")) passed++;
            else System.out.println("FAIL cycle msg=" + ex.getMessage());
        }
        eq(TopoSort.sort(new HashMap<>()), List.of(), "empty");
        System.out.println("PASSED=" + passed + " TOTAL=" + total);
    }
}
""",
    },
]

ROUNDS = 3


def _extract_java(text: str, cls: str) -> str:
    m = re.search(r"```(?:java)?\s*\n(.*?)```", text or "", re.DOTALL)
    src = m.group(1) if m else (text or "")
    # strip anything before the first class/import line
    lines = src.splitlines()
    for i, ln in enumerate(lines):
        if re.match(r"\s*(import|public\s+class|class)\b", ln):
            return "\n".join(lines[i:])
    return src


def _compile_and_test(src: str, cls: str, test_src: str) -> tuple[bool, int, int, str]:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / f"{cls}.java").write_text(src, encoding="utf-8")
        (d / "Test.java").write_text(test_src, encoding="utf-8")
        c = subprocess.run(["javac", f"{cls}.java", "Test.java"], cwd=td,
                           capture_output=True, text=True, timeout=60)
        if c.returncode != 0:
            return False, 0, 0, (c.stderr or c.stdout)[-2500:]
        r = subprocess.run(["java", "-cp", ".", "Test"], cwd=td,
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"PASSED=(\d+) TOTAL=(\d+)", out)
        if not m:
            return True, 0, 0, out[-2500:]
        return True, int(m.group(1)), int(m.group(2)), out[-2500:]


_OUR_SYS = (
    "You are a senior Java engineer. Produce COMPLETE, COMPILING Java "
    "source for the requested class — production quality, JDK 21, no "
    "external dependencies, no package declaration. Think through edge "
    "cases first, then output ONE ```java code block containing the full "
    "file and nothing else after it.")


def _run_arm(gen, task) -> tuple[int, int, int]:
    """Shared harness: identical for both arms. Returns (compiled_rounds_used,
    passed, total)."""
    feedback = ""
    best = (0, 0)
    for rnd in range(1, ROUNDS + 1):
        src = _extract_java(gen(task["spec"], task["test"], feedback), task["cls"])
        ok, p, t, out = _compile_and_test(src, task["cls"], task["test"])
        if ok and t and p == t:
            return rnd, p, t
        if (p, t) > best:
            best = (p, t)
        feedback = (("COMPILE ERROR:\n" if not ok else "TEST OUTPUT (fix the "
                     "failures):\n") + out
                    + "\n\nYOUR PREVIOUS SOURCE:\n" + src[:6000])
    return ROUNDS, best[0], best[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="not-needed")
    args = ap.parse_args()

    import httpx

    def ours_gen(spec: str, test_src: str, feedback: str) -> str:
        user = (f"TASK:\n{spec}\n\nYour class must pass this exact test file "
                f"(Test.java):\n```java\n{test_src}\n```\n"
                + (f"\nPREVIOUS ATTEMPT FEEDBACK:\n{feedback}\n" if feedback else ""))
        r = httpx.post(f"{args.base_url.rstrip('/')}/chat/completions",
                       json={"model": args.model, "temperature": 0,
                             "max_tokens": 4000,
                             "messages": [
                                 {"role": "system", "content": _OUR_SYS},
                                 {"role": "user", "content": user}]},
                       headers={"Authorization": f"Bearer {args.api_key}"},
                       timeout=600)
        return r.json()["choices"][0]["message"]["content"]

    import dspy
    dspy.configure(lm=dspy.LM(f"openai/{args.model}", api_base=args.base_url,
                              api_key=args.api_key, temperature=0,
                              max_tokens=4000, timeout=600))

    class WriteJava(dspy.Signature):
        """Write COMPLETE, COMPILING JDK-21 Java source for the requested
        class (no external deps, no package declaration) that passes the
        given test file. Reason about edge cases first."""
        spec: str = dspy.InputField()
        test_file: str = dspy.InputField()
        feedback: str = dspy.InputField(desc="compile/test errors from the previous attempt, empty on first try")
        java_source: str = dspy.OutputField(desc="the full .java file content")

    dspy_mod = dspy.ChainOfThought(WriteJava)

    def dspy_gen(spec: str, test_src: str, feedback: str) -> str:
        return dspy_mod(spec=spec, test_file=test_src,
                        feedback=feedback or "none").java_source

    print(f"{'task':<12} {'arm':<5} rounds  score")
    totals = {"ours": [0, 0], "dspy": [0, 0]}
    for task in TASKS:
        for arm, gen in (("ours", ours_gen), ("dspy", dspy_gen)):
            try:
                rnd, p, t = _run_arm(gen, task)
            except Exception as exc:  # noqa: BLE001
                print(f"{task['name']:<12} {arm:<5} ERROR {str(exc)[:90]}")
                continue
            totals[arm][0] += p
            totals[arm][1] += t or 1
            print(f"{task['name']:<12} {arm:<5} {rnd:>6}  {p}/{t}")
    print(f"\nTOTAL ours: {totals['ours'][0]}/{totals['ours'][1]}   "
          f"dspy: {totals['dspy'][0]}/{totals['dspy'][1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
