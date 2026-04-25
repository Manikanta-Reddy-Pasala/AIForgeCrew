"""External-tool eval harness for opencode (track X1).

Runs opencode against the same fixture YAML used by run_eval.py, so
metrics are comparable. Operates over SSH on the Mac Studio because
opencode + mlx_lm + the source repos live there.

Per fixture sample:
  1. Create a fresh git worktree of the fixture's project on Mac Studio.
  2. Drop AGENTS.md into the worktree (the "memory ON" condition).
  3. Run `opencode run --format json --dangerously-skip-permissions
        --dir <worktree> -m mlx-doer/<full-path> "<prompt>"`
  4. Parse the JSONL stream for tokens (step_finish.tokens) and tool
     usage (tool_use events).
  5. Run `mvn compile` in the worktree to verify the change compiles.
  6. Compute git diff stats.
  7. Write metrics JSON to evals/results/X1/{fixture}/{seq}.json.

We do NOT use the AIForge ticket DB — opencode runs detached from
graph-runner. Only the LM Studio mlx_lm endpoint is shared.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "evals" / "fixtures"
RESULTS_DIR = REPO_ROOT / "evals" / "results"
DEFAULT_SSH = os.environ.get("AIFORGE_EVAL_SSH", "manikanta@192.168.70.185")
DEFAULT_REPO_BASE = os.environ.get(
    "AIFORGE_EVAL_REPO_BASE",
    "/Users/manikanta/codeRepo",  # parent dir on Mac Studio for project repos
)
MLX_DOER_MODEL = (
    "mlx-doer//Users/manikanta/.lmstudio/models/lmstudio-community/"
    "Qwen3-Coder-Next-MLX-4bit"
)


def _ssh(host: str, cmd: str, *, capture: bool = True,
         timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", host, cmd],
        capture_output=capture, text=True, timeout=timeout, check=False,
    )


def _load_fixture(fid: str) -> dict:
    p = FIXTURES_DIR / f"{fid}.yaml"
    if not p.exists():
        sys.exit(f"fixture not found: {p}")
    return yaml.safe_load(p.read_text())


def _make_agents_md(fixture: dict) -> str:
    """The 'memory ON' file that opencode auto-loads. Keep it short
    project-context — same kind of guidance our doer system prompt has,
    so the comparison is fair."""
    return f"""# Repo: {fixture.get('project', '')}

This is a Java Spring Boot service. Use Maven (`mvn -pl {fixture.get('project','')} compile`) to verify changes compile. Stick to the existing pattern when adding logging or new endpoints — grep neighbouring controllers for examples.

## Working rules
- Make the smallest set of changes to satisfy the acceptance criteria.
- Do not add new dependencies.
- Run `mvn compile` after edits.
- When done, summarise what you changed in your final response.
"""


def _prompt_from_fixture(fixture: dict) -> str:
    return f"# {fixture['title']}\n\n{fixture['body']}"


def _make_worktree(host: str, project: str, run_id: str) -> str:
    repo = f"{DEFAULT_REPO_BASE}/{project}"
    worktree = f"/tmp/opencode-eval/{run_id}"
    branch = f"opencode-eval/{run_id}"
    cmd = (
        f"set -e; mkdir -p /tmp/opencode-eval; "
        f"cd {repo} && git fetch -q origin && "
        f"git worktree add -B {branch} {worktree} origin/master 2>&1 | tail -3 && "
        f"echo WORKTREE_OK"
    )
    cp = _ssh(host, cmd, timeout=120)
    if "WORKTREE_OK" not in cp.stdout:
        raise RuntimeError(f"worktree create failed: {cp.stdout}\n{cp.stderr}")
    return worktree


def _drop_agents_md(host: str, worktree: str, body: str) -> None:
    enc = body.replace("'", "'\\''")
    _ssh(host, f"cat > {worktree}/AGENTS.md <<'EOF'\n{body}\nEOF", timeout=20)


def _run_opencode(host: str, worktree: str, prompt: str,
                  out_path: str, timeout_s: int) -> dict:
    """Returns {"exit": int, "wall_s": float}. Streams JSONL to *out_path*."""
    enc_prompt = prompt.replace("'", "'\\''")
    cmd = (
        f"cd {worktree} && "
        f"opencode run --format json --dangerously-skip-permissions "
        f"-m '{MLX_DOER_MODEL}' '{enc_prompt}' > {out_path} 2>&1; "
        f"echo EXIT=$?"
    )
    t0 = time.time()
    cp = _ssh(host, cmd, timeout=timeout_s)
    wall = time.time() - t0
    exit_code = -1
    for line in cp.stdout.splitlines():
        if line.startswith("EXIT="):
            try:
                exit_code = int(line.split("=", 1)[1])
            except ValueError:
                pass
    return {"exit": exit_code, "wall_s": round(wall, 1)}


def _fetch_jsonl(host: str, remote_path: str) -> list[dict]:
    cp = _ssh(host, f"cat {remote_path}", timeout=60)
    out: list[dict] = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _git_diff_stat(host: str, worktree: str) -> dict:
    cp = _ssh(host,
              f"cd {worktree} && git status --short | head -30 && echo --- && "
              f"git diff --stat | tail -10",
              timeout=30)
    return {"raw": cp.stdout.strip()}


def _try_compile(host: str, worktree: str, project: str) -> dict:
    """Worktree IS the project root — `mvn -pl <project>` would fail
    with 'Could not find the selected project in the reactor'."""
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
    # `mvn -q` suppresses BUILD SUCCESS on green. MVN_EXIT echo is the
    # authoritative signal; failures still print BUILD FAILURE.
    return {
        "compile_pass": mvn_exit == 0 and "BUILD FAILURE" not in out,
        "mvn_exit": mvn_exit,
        "tail": out[-2000:],
    }


def _cleanup_worktree(host: str, project: str, worktree: str,
                      run_id: str) -> None:
    repo = f"{DEFAULT_REPO_BASE}/{project}"
    _ssh(host,
         f"cd {repo} && git worktree remove --force {worktree} 2>&1 | tail -3 && "
         f"git branch -D opencode-eval/{run_id} 2>&1 | tail -1",
         timeout=60)


def _compute_metrics(events: list[dict], wall_s: float,
                     compile_result: dict) -> dict:
    prompt_t = completion_t = total_t = 0
    cache_read = cache_write = 0
    step_count = tool_use_count = 0
    tool_dist: dict[str, int] = {}
    final_text = ""
    finish_reason = None
    for ev in events:
        et = ev.get("type")
        if et == "step_finish":
            step_count += 1
            tok = (ev.get("part") or {}).get("tokens") or {}
            prompt_t += int(tok.get("input") or 0)
            completion_t += int(tok.get("output") or 0)
            total_t += int(tok.get("total") or 0)
            cache = tok.get("cache") or {}
            cache_read += int(cache.get("read") or 0)
            cache_write += int(cache.get("write") or 0)
            finish_reason = (ev.get("part") or {}).get("reason") or finish_reason
        elif et == "tool_use" or et == "tool":
            tool_use_count += 1
            name = (ev.get("part") or {}).get("name") or "?"
            tool_dist[name] = tool_dist.get(name, 0) + 1
        elif et == "text":
            final_text = (ev.get("part") or {}).get("text") or final_text
    return {
        "compile_pass": bool(compile_result.get("compile_pass")),
        "wall_clock_s": round(wall_s, 1),
        "steps": step_count,
        "tool_use_count": tool_use_count,
        "prompt_tokens": prompt_t,
        "completion_tokens": completion_t,
        "total_tokens": total_t,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "tool_call_distribution": dict(sorted(
            tool_dist.items(), key=lambda kv: -kv[1])),
        "finish_reason": finish_reason,
        "final_text": final_text[:500],
    }


def _write_result(track: str, fixture_id: str, seq: int,
                  payload: dict) -> Path:
    out_dir = RESULTS_DIR / track / fixture_id
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"{seq:02d}_{ts}.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    return out_path


def run_one(fixture: dict, seq: int, host: str, timeout_s: int) -> dict:
    run_id = f"{fixture['id']}-{seq:02d}-{uuid.uuid4().hex[:6]}"
    track = "X1"
    print(f"[track={track} fixture={fixture['id']} seq={seq}] "
          f"run_id={run_id} host={host}")
    project = fixture["project"]
    remote_jsonl = f"/tmp/opencode-eval/{run_id}.jsonl"
    worktree = _make_worktree(host, project, run_id)
    print(f"  worktree={worktree}")
    try:
        _drop_agents_md(host, worktree, _make_agents_md(fixture))
        prompt = _prompt_from_fixture(fixture)
        run_meta = _run_opencode(host, worktree, prompt, remote_jsonl, timeout_s)
        print(f"  opencode exit={run_meta['exit']} wall={run_meta['wall_s']}s")
        events = _fetch_jsonl(host, remote_jsonl)
        compile_result = _try_compile(host, worktree, project)
        diff = _git_diff_stat(host, worktree)
        metrics = _compute_metrics(events, run_meta["wall_s"], compile_result)
    finally:
        _cleanup_worktree(host, project, worktree, run_id)
    payload = {
        "track": track,
        "fixture_id": fixture["id"],
        "fixture_name": fixture.get("name", ""),
        "seq": seq,
        "run_id": run_id,
        "metrics": metrics,
        "diff_stat": diff,
        "compile_tail": compile_result.get("tail", "")[-800:],
        "trace_event_count": len(events),
    }
    out_path = _write_result(track, fixture["id"], seq, payload)
    print(f"  done → {out_path}")
    print(f"  metrics: compile={metrics['compile_pass']} "
          f"toks={metrics['total_tokens']} "
          f"steps={metrics['steps']} "
          f"wall={metrics['wall_clock_s']}s")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", required=True, help="Fixture id (e.g. F1)")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--host", default=DEFAULT_SSH)
    ap.add_argument("--timeout", type=int, default=900,
                    help="Per-run timeout seconds (default 900)")
    args = ap.parse_args()
    fixture = _load_fixture(args.fixture)
    print(f"=== EVAL X1 (opencode) fixture={fixture['id']} "
          f"samples={args.samples} host={args.host} ===")
    for seq in range(1, args.samples + 1):
        try:
            run_one(fixture, seq, args.host, args.timeout)
        except KeyboardInterrupt:
            sys.exit("interrupted")
        except Exception as exc:
            print(f"  ERROR seq={seq}: {exc}")


if __name__ == "__main__":
    main()
