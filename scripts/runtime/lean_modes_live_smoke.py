#!/usr/bin/env python3
"""Matched Team vs Simple (and Plan) live smokes against a local LM.

Tasks: tiny | medium | hard — same prompt + fresh repo seed per mode.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Still lean if verifier/refiner run — those are part of the default Team path.
# Fail only when the old hour-cost enhancer/context fan-out appears.
BANNED = {"enhancer", "researcher",
          "ctx_repomap", "ctx_conventions", "gap_eval"}


def _check_tiny(repo: Path) -> bool:
    return "def multiply" in (repo / "mathutil.py").read_text()


def _check_medium(repo: Path) -> bool:
    stats = repo / "statsutil.py"
    test = repo / "test_statsutil.py"
    if not stats.is_file() or not test.is_file():
        return False
    text = stats.read_text()
    if "def mean" not in text or "def median" not in text:
        return False
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(test)],
        cwd=str(repo), capture_output=True, text=True, timeout=120,
    )
    return r.returncode == 0


def _check_hard(repo: Path) -> bool:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_trigger.py"],
        cwd=str(repo), capture_output=True, text=True, timeout=120,
    )
    return r.returncode == 0


TASKS = {
    "tiny": {
        "prompt": (
            "In mathutil.py add a multiply(a, b) function that returns a*b. "
            "Only touch that one file. Keep it tiny."
        ),
        "check": _check_tiny,
        "seed": "tiny",
    },
    "medium": {
        "prompt": (
            "Add a small stats helper package in this repo:\n"
            "1) Create statsutil.py with mean(nums) and median(nums) "
            "(empty list raises ValueError).\n"
            "2) Create test_statsutil.py with pytest tests for mean, median, "
            "and the empty-list error.\n"
            "3) Run pytest -q and leave tests passing.\n"
            "Do not add unrelated files."
        ),
        "check": _check_medium,
        "seed": "medium",
    },
    "hard": {
        "prompt": (
            "This repo has a tiny SQL dialect stub in dialect/\n"
            "Implement CREATE TRIGGER support for Postgres-like SQL:\n"
            "1) In dialect/tokens.py add TokenType.TRIGGER and KEYWORDS['TRIGGER'].\n"
            "2) In dialect/parser.py parse "
            "`CREATE TRIGGER name ON table FOR EACH ROW EXECUTE function_name` "
            "into a Trigger node (name, table, function).\n"
            "3) In dialect/generator.py emit the same SQL back.\n"
            "4) Make tests/test_trigger.py pass "
            "(round-trip identity for one statement).\n"
            "Run: python -m pytest tests/test_trigger.py -q"
        ),
        "check": _check_hard,
        "seed": "hard",
    },
}


def _post(base: str, path: str, body: dict | None = None, timeout: float = 30):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base}{path}", data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def _sse(base: str, path: str, body: dict, timeout: float = 900) -> list[dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base}{path}", data=data,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    events: list[dict] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                block, buf = buf.split(b"\n\n", 1)
                for line in block.split(b"\n"):
                    if not line.startswith(b"data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == b"[DONE]":
                        continue
                    try:
                        events.append(json.loads(raw.decode()))
                    except json.JSONDecodeError:
                        pass
    return events


def _role_names(events: list[dict]) -> set[str]:
    names: set[str] = set()
    for e in events:
        for k in ("role", "agent", "agent_role", "name"):
            v = e.get(k)
            if isinstance(v, str) and v:
                names.add(v.lower())
        st = e.get("stage") or e.get("step")
        if isinstance(st, dict):
            for k in ("role", "agent", "name"):
                v = st.get(k)
                if isinstance(v, str) and v:
                    names.add(v.lower())
    return names


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)


def _commit_all(path: Path, msg: str = "init") -> None:
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", msg], check=True)


def _seed_tiny(path: Path) -> Path:
    _git_init(path)
    (path / "mathutil.py").write_text("def add(a, b):\n    return a + b\n")
    _commit_all(path)
    return path


def _seed_medium(path: Path) -> Path:
    _git_init(path)
    (path / "README.md").write_text("# stats scratch\n")
    _commit_all(path)
    return path


def _seed_hard(path: Path) -> Path:
    _git_init(path)
    (path / "dialect").mkdir()
    (path / "tests").mkdir()
    (path / "dialect" / "__init__.py").write_text("")
    (path / "dialect" / "tokens.py").write_text(
        "from enum import Enum, auto\n\n"
        "class TokenType(Enum):\n"
        "    CREATE = auto()\n"
        "    IDENT = auto()\n"
        "    ON = auto()\n"
        "    FOR = auto()\n"
        "    EACH = auto()\n"
        "    ROW = auto()\n"
        "    EXECUTE = auto()\n"
        "    EOF = auto()\n\n"
        "KEYWORDS = {\n"
        "    'CREATE': TokenType.CREATE,\n"
        "    'ON': TokenType.ON,\n"
        "    'FOR': TokenType.FOR,\n"
        "    'EACH': TokenType.EACH,\n"
        "    'ROW': TokenType.ROW,\n"
        "    'EXECUTE': TokenType.EXECUTE,\n"
        "}\n"
    )
    (path / "dialect" / "parser.py").write_text(
        "from .tokens import KEYWORDS, TokenType\n\n"
        "class Trigger:\n"
        "    def __init__(self, name, table, function):\n"
        "        self.name, self.table, self.function = name, table, function\n\n"
        "def tokenize(sql: str):\n"
        "    out = []\n"
        "    for w in sql.replace('(', ' ( ').replace(')', ' ) ').split():\n"
        "        u = w.upper()\n"
        "        out.append((KEYWORDS.get(u, TokenType.IDENT), w))\n"
        "    out.append((TokenType.EOF, ''))\n"
        "    return out\n\n"
        "def parse(sql: str):\n"
        "    raise NotImplementedError('CREATE TRIGGER not implemented')\n"
    )
    (path / "dialect" / "generator.py").write_text(
        "def generate(node) -> str:\n"
        "    raise NotImplementedError('CREATE TRIGGER not implemented')\n"
    )
    (path / "tests" / "test_trigger.py").write_text(
        "from dialect.parser import parse\n"
        "from dialect.generator import generate\n\n"
        "SQL = ('CREATE TRIGGER log_changes ON users "
        "FOR EACH ROW EXECUTE log_fn')\n\n"
        "def test_roundtrip():\n"
        "    node = parse(SQL)\n"
        "    assert node.name.lower() == 'log_changes'\n"
        "    assert node.table.lower() == 'users'\n"
        "    assert node.function.lower() == 'log_fn'\n"
        "    assert generate(node).split() == SQL.split()\n"
    )
    _commit_all(path)
    return path


SEEDS = {"tiny": _seed_tiny, "medium": _seed_medium, "hard": _seed_hard}


def run_one(base: str, task: str, mode: str, root: Path, timeout: float) -> dict:
    spec = TASKS[task]
    repo = SEEDS[spec["seed"]](root / f"{task}-{mode}")
    sess = _post(base, "/api/chat/sessions", {
        "cwd": str(repo), "title": f"{task}-{mode}",
    })
    sid = sess["id"]
    t0 = time.time()
    events = _sse(base, f"/api/chat/sessions/{sid}/message", {
        "content": spec["prompt"], "mode": mode,
    }, timeout=timeout)
    wall = time.time() - t0
    roles = _role_names(events)
    banned_hit = sorted(roles & BANNED)
    code_ok = bool(spec["check"](repo)) if spec["check"] else False
    lean_ok = mode != "team" or not banned_hit
    types = [e.get("type") for e in events]
    ok = code_ok and lean_ok and ("done" in types or "message" in types)
    return {
        "task": task, "mode": mode, "ok": ok, "code_ok": code_ok,
        "lean_ok": lean_ok, "wall_s": round(wall, 1), "sid": sid,
        "banned_seen": banned_hit, "roles_seen": sorted(roles)[:40],
        "event_count": len(events), "types_tail": types[-12:],
        "repo": str(repo),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--host-tag", default="host")
    ap.add_argument("--repo-root", default="/tmp")
    ap.add_argument("--modes", default="team,simple")
    ap.add_argument("--tasks", default="tiny")
    ap.add_argument("--timeout", type=float, default=900)
    args = ap.parse_args()
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    root = Path(args.repo_root) / f"mode-cmp-{args.host_tag}-{int(time.time())}"
    results = []
    for task in tasks:
        if task not in TASKS:
            print(f"unknown task {task}", file=sys.stderr)
            return 2
        for mode in modes:
            print(f"[{args.host_tag}] RUN task={task} mode={mode}", flush=True)
            r = run_one(args.base, task, mode, root, args.timeout)
            results.append(r)
            print(json.dumps(r, indent=2), flush=True)
    print("COMPARE", flush=True)
    print(json.dumps(results, indent=2), flush=True)
    print("| task | mode | wall_s | ok | code_ok | lean_ok |", flush=True)
    print("|---|---|---:|---|---|---|", flush=True)
    for r in results:
        print(
            f"| {r['task']} | {r['mode']} | {r['wall_s']} | {r['ok']} | "
            f"{r['code_ok']} | {r['lean_ok']} |",
            flush=True,
        )
    failed = [r for r in results if not r.get("ok")]
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as e:
        print(f"URL_ERROR {e}", file=sys.stderr)
        raise SystemExit(2)
