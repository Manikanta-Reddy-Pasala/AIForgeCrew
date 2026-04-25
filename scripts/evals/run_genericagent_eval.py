"""External-tool eval harness for GenericAgent (track X2).

Runs GenericAgent against the same fixture YAML used by run_eval.py, so
metrics are comparable. Operates over SSH on the Mac Studio because
GenericAgent + mlx_lm + the source repos all live there.

Per fixture sample:
  1. Create a fresh git worktree of the fixture's project on Mac Studio.
  2. Build a prompt that prepends `cd <worktree>` so GenericAgent's
     `code_run` shell tool operates inside the worktree.
  3. Drop the prompt into ~/genericagent/temp/<run_id>/input.txt and
     launch agentmain.py in --task mode (non-interactive). It reads
     input.txt, drives the agent loop, and writes output.txt with a
     "[ROUND END]" sentinel when the run terminates.
  4. Tail/parse the output.txt + the model_responses log for tool
     calls and final answer.
  5. Run `mvn compile` in the worktree to verify the change compiles.
  6. Compute git diff stats.
  7. Write metrics JSON to evals/results/X2/{fixture}/{seq}.json.

Like X1 (opencode), we do NOT use the AIForge ticket DB — GenericAgent
runs detached from graph-runner. Only the LM Studio mlx_lm endpoint is
shared.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aiforge_core.agents import AgentContract, load_agents
from aiforge_core.eval.rule_checker import check_run


FIXTURES_DIR = REPO_ROOT / "evals" / "fixtures"
RESULTS_DIR = REPO_ROOT / "evals" / "results"
DEFAULT_SSH = os.environ.get("AIFORGE_EVAL_SSH", "manikanta@192.168.70.185")
DEFAULT_REPO_BASE = os.environ.get(
    "AIFORGE_EVAL_REPO_BASE", "/Users/manikanta/codeRepo",
)
GA_DIR = "/Users/manikanta/genericagent"


def _ssh(host: str, cmd: str, *, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", host, cmd],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def _load_fixture(fid: str) -> dict:
    p = FIXTURES_DIR / f"{fid}.yaml"
    if not p.exists():
        sys.exit(f"fixture not found: {p}")
    return yaml.safe_load(p.read_text())


def _make_worktree(host: str, project: str, run_id: str) -> str:
    repo = f"{DEFAULT_REPO_BASE}/{project}"
    worktree = f"/tmp/ga-eval/{run_id}"
    branch = f"ga-eval/{run_id}"
    cmd = (
        f"set -e; mkdir -p /tmp/ga-eval; "
        f"cd {repo} && git fetch -q origin && "
        f"git worktree add -B {branch} {worktree} origin/master 2>&1 | tail -3 && "
        f"echo WORKTREE_OK"
    )
    cp = _ssh(host, cmd, timeout=120)
    if "WORKTREE_OK" not in cp.stdout:
        raise RuntimeError(f"worktree create failed: {cp.stdout}\n{cp.stderr}")
    return worktree


def _prompt_from_fixture(fixture: dict, worktree: str,
                         no_memory: bool = False) -> str:
    """Build prompt prepended with worktree cd-instruction so the
    code_run shell tool operates against the right files. When
    *no_memory* is set, append an instruction that suppresses GA's
    `start_long_term_update` tool — used to isolate memory-crystallization
    cost from real task work."""
    extra = ""
    if no_memory:
        extra = ("\n\n## Constraint\nDo NOT call `start_long_term_update` "
                 "during this task. Skip all memory-crystallization steps. "
                 "Stop as soon as the build is green.")
    return (
        f"Working directory: `{worktree}` — every shell command must be run "
        f"there (start with `cd {worktree} && ...` or stay in that dir).\n\n"
        f"# {fixture['title']}\n\n{fixture['body']}\n\n"
        f"Verify with `cd {worktree} && mvn -DskipTests compile` "
        f"and report the final BUILD result in your last message."
        f"{extra}"
    )


def _launch_ga(host: str, run_id: str, prompt: str,
               timeout_s: int) -> dict:
    """Drop prompt into temp/<run_id>/input.txt, run agentmain.py
    with --task <run_id> in the background, then poll output.txt for the
    "[ROUND END]" sentinel. agentmain's --task mode otherwise hangs 10
    min waiting for a reply.txt follow-up that we'll never send.

    Returns {"exit": int, "wall_s": float, ...}."""
    task_dir = f"{GA_DIR}/temp/{run_id}"
    output_path = f"{task_dir}/output.txt"
    pid_path = f"{task_dir}/agentmain.pid"
    log_path = f"{task_dir}/agentmain.log"

    setup_cmd = (
        f"set -e; mkdir -p {task_dir}; "
        f"cat > {task_dir}/input.txt <<'EOF_PROMPT'\n{prompt}\nEOF_PROMPT\n"
        f"cd {GA_DIR} && nohup env GA_LANG=en python3 agentmain.py "
        f"--task {run_id} --llm_no 0 > {log_path} 2>&1 & "
        f"echo $! > {pid_path}; cat {pid_path}"
    )
    cp = _ssh(host, setup_cmd, timeout=30)
    pid_str = cp.stdout.strip().splitlines()[-1] if cp.stdout.strip() else ""
    if not pid_str.isdigit():
        raise RuntimeError(f"failed to launch agentmain: {cp.stdout}\n{cp.stderr}")
    print(f"  agentmain pid={pid_str} log={log_path}")

    t0 = time.time()
    deadline = t0 + timeout_s
    seen_round_end = False
    while time.time() < deadline:
        time.sleep(8)
        check = _ssh(
            host,
            f"test -f {output_path} && grep -q '\\[ROUND END\\]' {output_path} && echo DONE || echo PENDING",
            timeout=15,
        )
        if "DONE" in check.stdout:
            seen_round_end = True
            break
    wall = time.time() - t0

    # Kill the agentmain process — it would otherwise wait 10 min for reply.txt.
    _ssh(host, f"kill {pid_str} 2>/dev/null; sleep 1; kill -9 {pid_str} 2>/dev/null || true",
         timeout=15)

    log_tail = _ssh(host, f"tail -200 {log_path} 2>/dev/null || true",
                    timeout=15).stdout
    return {
        "exit": 0 if seen_round_end else -2,
        "wall_s": round(wall, 1),
        "round_end_seen": seen_round_end,
        "stdout_tail": log_tail[-3000:],
        "stderr_tail": "",
    }


def _fetch_output(host: str, run_id: str) -> str:
    cp = _ssh(host, f"cat {GA_DIR}/temp/{run_id}/output.txt 2>&1 || true",
              timeout=60)
    return cp.stdout


def _git_diff_stat(host: str, worktree: str) -> dict:
    cp = _ssh(host,
              f"cd {worktree} && git status --short | head -30 && echo --- && "
              f"git diff --stat | tail -10",
              timeout=30)
    return {"raw": cp.stdout.strip()}


def _verify_expected_files(host: str, worktree: str, fixture: dict) -> dict:
    """Assert each path in expected.files_modified_must_include actually
    exists in the worktree. Catches the false-green case where the model's
    file_write silently failed (e.g. relative path resolved outside
    worktree, or content body missing)."""
    must = (fixture.get("expected") or {}).get("files_modified_must_include") or []
    missing: list[str] = []
    present: list[str] = []
    for rel in must:
        # Fixture paths are repo-relative (start with "PosClientBackend/...").
        # Strip the leading project dir since the worktree IS the project root.
        rel_in_worktree = rel
        prefix = (fixture.get("project") or "") + "/"
        if rel_in_worktree.startswith(prefix):
            rel_in_worktree = rel_in_worktree[len(prefix):]
        full = f"{worktree}/{rel_in_worktree}"
        cp = _ssh(host, f"test -f '{full}' && echo YES || echo NO", timeout=15)
        (present if cp.stdout.strip().endswith("YES") else missing).append(rel)
    return {
        "expected_count": len(must),
        "present": present,
        "missing": missing,
        "all_present": len(missing) == 0,
    }


def _try_compile(host: str, worktree: str, project: str) -> dict:
    """The fixture worktree IS the project root (PosClientBackend repo
    is itself a Maven project, not a submodule of a parent reactor). So
    plain `mvn compile` is correct — `-pl <project>` would fail with
    'Could not find the selected project in the reactor'.

    Also runs `mvn test-compile` so test classes (if any) are verified.
    """
    cp = _ssh(host,
              f"cd {worktree} && mvn -q -DskipTests compile 2>&1; echo MVN_EXIT=$?",
              timeout=900)
    out = cp.stdout
    mvn_exit = -1
    for line in out.splitlines():
        if line.startswith("MVN_EXIT="):
            try:
                mvn_exit = int(line.split("=", 1)[1])
            except ValueError:
                pass
    main_pass = mvn_exit == 0 and "BUILD FAILURE" not in out

    # Test-compile (only if main passed and a src/test exists).
    test_exit = None
    test_tail = ""
    test_pass = None
    has_tests_check = _ssh(host,
                           f"test -d {worktree}/src/test/java && echo YES || echo NO",
                           timeout=15).stdout.strip()
    if main_pass and has_tests_check.endswith("YES"):
        cp2 = _ssh(host,
                   f"cd {worktree} && mvn -q -DskipTests test-compile 2>&1; echo MVN_EXIT=$?",
                   timeout=900)
        out2 = cp2.stdout
        for line in out2.splitlines():
            if line.startswith("MVN_EXIT="):
                try:
                    test_exit = int(line.split("=", 1)[1])
                except ValueError:
                    pass
        test_pass = test_exit == 0 and "BUILD FAILURE" not in out2
        test_tail = out2[-2000:]

    return {
        "compile_pass": main_pass,
        "mvn_exit": mvn_exit,
        "tail": out[-2000:],
        "test_compile_pass": test_pass,
        "test_compile_exit": test_exit,
        "test_compile_tail": test_tail,
    }


def _cleanup(host: str, project: str, worktree: str, run_id: str) -> None:
    repo = f"{DEFAULT_REPO_BASE}/{project}"
    _ssh(host,
         f"cd {repo} && git worktree remove --force {worktree} 2>&1 | tail -3 && "
         f"git branch -D ga-eval/{run_id} 2>&1 | tail -1",
         timeout=60)


_TOOL_MARKER_RX = re.compile(r"🛠️\s*Tool:\s*`([a-z_]+)`")
_TOOL_COMPACT_RX = re.compile(r"🛠️\s+([a-z_]+)\(")
_TURN_RX = re.compile(r"LLM Running \(Turn (\d+)\)")


def _compute_metrics(output_text: str, wall_s: float,
                     compile_result: dict,
                     role: str | None = None,
                     contract: AgentContract | None = None) -> dict:
    """Best-effort metric extraction from GenericAgent's output stream.
    GA does not emit token usage natively; we count turns and tool calls
    and char-estimate tokens via output length (cross-track ratio only).

    When *role* and *contract* are provided, the result also carries
    ``rule_check_passed`` and ``rule_violations`` from
    :func:`aiforge_core.eval.rule_checker.check_run`.
    """
    tool_dist: dict[str, int] = {}
    for m in _TOOL_MARKER_RX.finditer(output_text):
        n = m.group(1)
        tool_dist[n] = tool_dist.get(n, 0) + 1
    if not tool_dist:
        for m in _TOOL_COMPACT_RX.finditer(output_text):
            n = m.group(1)
            tool_dist[n] = tool_dist.get(n, 0) + 1
    turns = 0
    for m in _TURN_RX.finditer(output_text):
        turns = max(turns, int(m.group(1)))
    char_count = len(output_text)
    estimated_total_tokens = char_count // 4
    metrics: dict = {
        "compile_pass": bool(compile_result.get("compile_pass")),
        "wall_clock_s": round(wall_s, 1),
        "steps": turns,
        "tool_use_count": sum(tool_dist.values()),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": estimated_total_tokens,
        "tokens_estimated": True,
        "tool_call_distribution": dict(sorted(
            tool_dist.items(), key=lambda kv: -kv[1])),
        "round_end_seen": "[ROUND END]" in output_text,
    }
    if role is not None and contract is not None:
        events: list[dict] = []
        for name, count in tool_dist.items():
            for _ in range(count):
                events.append({"tool_name": name})
        rc = check_run(role, events, contract,
                       wall_clock_s=wall_s, turn_count=turns)
        metrics["rule_check_passed"] = rc.passed
        metrics["rule_violations"] = list(rc.violations)
        metrics["rule_check_stats"] = rc.stats
    else:
        metrics["rule_check_passed"] = None
        metrics["rule_violations"] = []
    return metrics


def _write_result(track: str, fixture_id: str, seq: int,
                  payload: dict) -> Path:
    out_dir = RESULTS_DIR / track / fixture_id
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"{seq:02d}_{ts}.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    return out_path


def _load_role_contract(role: str) -> AgentContract | None:
    try:
        return load_agents()[role]
    except Exception as exc:
        print(f"  WARN: cannot load contract for role={role}: {exc}")
        return None


def run_one(fixture: dict, seq: int, host: str, timeout_s: int,
            role: str = "doer") -> dict:
    run_id = f"{fixture['id']}-{seq:02d}-{uuid.uuid4().hex[:6]}"
    track = "X2"
    print(f"[track={track} fixture={fixture['id']} seq={seq}] "
          f"run_id={run_id} host={host} role={role}")
    project = fixture["project"]
    contract = _load_role_contract(role)
    worktree = _make_worktree(host, project, run_id)
    print(f"  worktree={worktree}")
    try:
        prompt = _prompt_from_fixture(fixture, worktree)
        meta = _launch_ga(host, run_id, prompt, timeout_s)
        print(f"  ga exit={meta['exit']} wall={meta['wall_s']}s")
        output_text = _fetch_output(host, run_id)
        compile_result = _try_compile(host, worktree, project)
        diff = _git_diff_stat(host, worktree)
        metrics = _compute_metrics(output_text, meta["wall_s"],
                                   compile_result, role=role,
                                   contract=contract)
    finally:
        _cleanup(host, project, worktree, run_id)
    payload = {
        "track": track,
        "fixture_id": fixture["id"],
        "fixture_name": fixture.get("name", ""),
        "seq": seq,
        "run_id": run_id,
        "metrics": metrics,
        "diff_stat": diff,
        "compile_tail": compile_result.get("tail", "")[-800:],
        "stdout_tail": meta.get("stdout_tail", "")[-1500:],
        "stderr_tail": meta.get("stderr_tail", "")[-800:],
        "output_len": len(output_text),
    }
    out_path = _write_result(track, fixture["id"], seq, payload)
    print(f"  done → {out_path}")
    print(f"  metrics: compile={metrics['compile_pass']} "
          f"toks~{metrics['total_tokens']} "
          f"steps={metrics['steps']} tools={metrics['tool_use_count']} "
          f"wall={metrics['wall_clock_s']}s")
    return payload


def run_one_keep(fixture: dict, seq: int, host: str, timeout_s: int,
                 worktree: str, run_id: str, role: str = "doer") -> dict:
    """Run a fixture on an EXISTING worktree (for chained subticket runs).
    Skips worktree create + skips final cleanup so callers can chain."""
    track = "X2"
    print(f"[track={track} fixture={fixture['id']} seq={seq}] "
          f"run_id={run_id} (keep worktree) role={role}")
    project = fixture["project"]
    contract = _load_role_contract(role)
    print(f"  worktree={worktree}")
    prompt = _prompt_from_fixture(fixture, worktree)
    meta = _launch_ga(host, run_id, prompt, timeout_s)
    print(f"  ga exit={meta['exit']} wall={meta['wall_s']}s")
    output_text = _fetch_output(host, run_id)
    compile_result = _try_compile(host, worktree, project)
    diff = _git_diff_stat(host, worktree)
    files_check = _verify_expected_files(host, worktree, fixture)
    metrics = _compute_metrics(output_text, meta["wall_s"], compile_result,
                               role=role, contract=contract)
    metrics["expected_files_present"] = files_check["all_present"]
    metrics["expected_files_missing"] = files_check["missing"]
    rule_ok = metrics.get("rule_check_passed")
    if rule_ok is None:
        rule_ok = True
    metrics["task_pass"] = (
        metrics["compile_pass"]
        and files_check["all_present"]
        and bool(rule_ok)
    )
    payload = {
        "track": track,
        "fixture_id": fixture["id"],
        "fixture_name": fixture.get("name", ""),
        "seq": seq,
        "run_id": run_id,
        "metrics": metrics,
        "diff_stat": diff,
        "compile_tail": compile_result.get("tail", "")[-800:],
        "test_compile_pass": compile_result.get("test_compile_pass"),
        "test_compile_tail": compile_result.get("test_compile_tail", "")[-800:],
        "stdout_tail": meta.get("stdout_tail", "")[-1500:],
        "stderr_tail": meta.get("stderr_tail", "")[-800:],
        "output_len": len(output_text),
    }
    out_path = _write_result("X2", fixture["id"], seq, payload)
    print(f"  done → {out_path}")
    print(f"  metrics: compile={metrics['compile_pass']} "
          f"toks~{metrics['total_tokens']} "
          f"steps={metrics['steps']} tools={metrics['tool_use_count']} "
          f"wall={metrics['wall_clock_s']}s")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", help="Fixture id (e.g. F1) — single mode")
    ap.add_argument("--chain", help="Comma-separated fixture ids run on the "
                    "same worktree, e.g. F7a,F7b,F7c")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--host", default=DEFAULT_SSH)
    ap.add_argument("--timeout", type=int, default=900,
                    help="Per-run timeout seconds (default 900)")
    ap.add_argument("--role", default="doer",
                    help="agents.yaml role whose contract gates this run "
                    "(default: doer)")
    args = ap.parse_args()
    if args.chain:
        ids = [x.strip() for x in args.chain.split(",") if x.strip()]
        fixtures = [_load_fixture(fid) for fid in ids]
        # All chain fixtures must share the same project.
        project = fixtures[0]["project"]
        for f in fixtures:
            if f["project"] != project:
                sys.exit("chain fixtures must share project")
        run_id = f"CHAIN-{ids[0]}-{uuid.uuid4().hex[:6]}"
        worktree = _make_worktree(args.host, project, run_id)
        print(f"=== CHAIN {ids} project={project} worktree={worktree} ===")
        try:
            for i, fixture in enumerate(fixtures, start=1):
                sub_run_id = f"{run_id}-{fixture['id']}"
                payload = run_one_keep(fixture, i, args.host, args.timeout,
                                       worktree, sub_run_id, role=args.role)
                # Short-circuit on failure — broken state poisons downstream
                # subtickets. Skip remaining + record skip stub.
                if not payload["metrics"].get("task_pass"):
                    print(f"  CHAIN STOP: {fixture['id']} did not pass "
                          f"(task_pass={payload['metrics'].get('task_pass')}). "
                          f"Skipping remaining {len(fixtures) - i} subticket(s).")
                    for j in range(i + 1, len(fixtures) + 1):
                        skipped = fixtures[j - 1]
                        skip_payload = {
                            "track": "X2",
                            "fixture_id": skipped["id"],
                            "fixture_name": skipped.get("name", ""),
                            "seq": j,
                            "run_id": f"{run_id}-{skipped['id']}-SKIPPED",
                            "metrics": {"task_pass": False, "skipped": True,
                                        "reason": f"prior subticket {fixture['id']} failed"},
                            "diff_stat": {"raw": ""},
                            "compile_tail": "",
                        }
                        out_path = _write_result("X2", skipped["id"], j, skip_payload)
                        print(f"  skipped → {out_path}")
                    break
        finally:
            _cleanup(args.host, project, worktree, run_id)
        return
    if not args.fixture:
        sys.exit("--fixture or --chain required")
    fixture = _load_fixture(args.fixture)
    print(f"=== EVAL X2 (GenericAgent) fixture={fixture['id']} "
          f"samples={args.samples} host={args.host} ===")
    for seq in range(1, args.samples + 1):
        try:
            run_one(fixture, seq, args.host, args.timeout, role=args.role)
        except KeyboardInterrupt:
            sys.exit("interrupted")
        except Exception as exc:
            print(f"  ERROR seq={seq}: {exc}")


if __name__ == "__main__":
    main()
