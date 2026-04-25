"""EVAL-1b: Planner backend A/B — smolagents CodeAgent vs GenericAgent text-protocol.

For each fixture sample:
  - smolagents_codeagent backend: build planner via aiforge_core.planner.agent.build_planner_agent,
    monkey-patch the Postgres write so write_plan dumps to a local file.
  - genericagent_text_protocol backend: invoke GA via agentmain.py --task --llm_no 1 with a
    Planner preamble that asks the model to produce a plan via file_write to plan.md.

Both backends operate on a fresh git worktree so grep/list/read tools have something to
look at. Writes a JSON sidecar per run with metrics, captured plan, and tool distribution.

Run on NUC. Both backends share the same LM Studio endpoint (port 1235) for the Planner
model, configured the same way the production runtime uses.
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
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURES_DIR = REPO_ROOT / "evals" / "fixtures"
RESULTS_DIR = REPO_ROOT / "evals" / "results" / "EVAL-1b"
GA_DIR = "/home/mani/genericagent"
NUC_REPO_BASE = "/home/mani/codeRepo"


# ─────────────────────────────────────────────────────────────────────────
# Stub Ticket — looks enough like aiforge_core.runtime.tickets.Ticket
# for planner tool factories to read off it.
# ─────────────────────────────────────────────────────────────────────────
@dataclass
class _FakeTicket:
    id: int
    identifier: str
    title: str
    body: str
    project: str | None = None
    parent_id: int | None = None
    status: str = "in_progress"
    priority: str = "medium"
    assignee_role: str = "planner"
    branch: str | None = None
    labels: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def _load_fixture(fid: str) -> dict:
    p = FIXTURES_DIR / f"{fid}.yaml"
    if not p.exists():
        sys.exit(f"fixture not found: {p}")
    return yaml.safe_load(p.read_text())


# ─────────────────────────────────────────────────────────────────────────
# Plan quality heuristic
# ─────────────────────────────────────────────────────────────────────────
_REQUIRED_SECTIONS = ["files", "plan", "signatures", "pitfalls"]


def _score_plan(plan_text: str, fixture: dict) -> dict:
    body = (plan_text or "").lower()
    sections_present = {}
    for sec in _REQUIRED_SECTIONS:
        # accept either "## Files", "## files", or markdown-style "files:" block
        sections_present[sec] = bool(
            re.search(rf"^##+\s*{sec}\b", plan_text, re.MULTILINE | re.IGNORECASE)
            or re.search(rf"^{sec}:", plan_text, re.MULTILINE | re.IGNORECASE)
        )
    must_files = (fixture.get("expected") or {}).get("files_modified_must_include") or []
    file_mentions = []
    for f in must_files:
        # accept either full path or just basename
        basename = f.rsplit("/", 1)[-1]
        if basename in plan_text or f in plan_text:
            file_mentions.append(f)
    quality_score = (
        sum(1 for v in sections_present.values() if v) / max(1, len(sections_present))
    )
    has_target_files = len(file_mentions) == len(must_files) and len(must_files) > 0
    return {
        "sections_present": sections_present,
        "quality_score": round(quality_score, 2),
        "expected_files_mentioned": file_mentions,
        "expected_files_total": len(must_files),
        "all_target_files_mentioned": has_target_files,
    }


# ─────────────────────────────────────────────────────────────────────────
# Backend 1: smolagents CodeAgent
# ─────────────────────────────────────────────────────────────────────────
def _run_smolagents_planner(fixture: dict, run_id: str, worktree: str,
                            results_dir: Path) -> dict:
    """Build a smolagents CodeAgent planner directly. Monkey-patch the write_plan
    Postgres call so plan goes to a local file."""
    import os as _os
    # Configure environment before importing aiforge_core modules.
    _os.environ.setdefault("AIFORGE_DSN", "postgresql://nope:nope@127.0.0.1:65535/nope")
    _os.environ["AIFORGE_PLANNER_BASE_URL"] = _os.environ.get(
        "AIFORGE_PLANNER_BASE_URL", "http://127.0.0.1:1235/v1"
    )
    _os.environ["AIFORGE_PLANNER_MODEL"] = _os.environ.get(
        "AIFORGE_PLANNER_MODEL",
        "/Users/manikanta/.lmstudio/models/unsloth/Qwen3.6-27B-UD-MLX-4bit",
    )
    _os.environ["AIFORGE_PLANNER_API_KEY"] = "sk-local"
    _os.environ["AIFORGE_PLANNER_BACKEND"] = "code"
    _os.environ["WORKTREE_ROOT"] = str(Path(worktree).parent)

    from aiforge_core.planner import agent as planner_agent
    from aiforge_core.planner import tools as planner_tools

    captured_plan = {"text": "", "files": [], "signatures": "", "pitfalls": "", "cross_service": ""}
    subticket_log: list[dict] = []

    # Replace write_plan factory with a local one that writes to plan.md + dict.
    def _make_write_plan_local(ctx: dict):
        from smolagents import tool

        @tool
        def write_plan(files: list, plan: str, signatures: str = "",
                       pitfalls: str = "", cross_service: str = "") -> str:
            """Persist the plan locally for the eval harness.

            Args:
                files: List of file paths the Doer will edit.
                plan: Numbered high-level plan in plain text.
                signatures: Verified method signatures (path:line: <sig>).
                pitfalls: Compile pitfalls to avoid.
                cross_service: Cross-service coordination notes.
            """
            captured_plan["files"] = list(files or [])
            captured_plan["plan"] = plan
            captured_plan["signatures"] = signatures
            captured_plan["pitfalls"] = pitfalls
            captured_plan["cross_service"] = cross_service
            md = (
                f"## Files\n" + "".join(f"- {f}\n" for f in files)
                + f"\n## Plan\n{plan}\n"
                + (f"\n## Signatures\n{signatures}\n" if signatures else "")
                + (f"\n## Pitfalls\n{pitfalls}\n" if pitfalls else "")
                + (f"\n## Cross-service\n{cross_service}\n" if cross_service else "")
            )
            captured_plan["text"] = md
            (results_dir / f"{run_id}_plan.md").write_text(md)
            return f"OK (eval): plan captured ({len(files)} files, {len(plan)} chars)"
        return write_plan

    def _make_create_child_ticket_local(ctx: dict):
        from smolagents import tool

        @tool
        def create_child_ticket(title: str, body: str, project: str,
                                assignee_role: str = "planner") -> str:
            """Capture child ticket creations (eval harness — no DB).

            Args:
                title: Short title for the child ticket.
                body: Detailed body for the child ticket.
                project: Project key.
                assignee_role: Role that should process the child ticket.
            """
            n = len(subticket_log) + 1
            ident = f"FAKE-{run_id[-6:]}-{n:02d}"
            subticket_log.append({
                "identifier": ident, "title": title, "body": body[:500],
                "project": project, "assignee_role": assignee_role,
            })
            return ident
        return create_child_ticket

    # Disable search_memory + lookup_repo (need infra we don't have)
    def _make_noop_search_memory(ctx: dict):
        from smolagents import tool

        @tool
        def search_memory(query: str, role: str = "planner", top_k: int = 10) -> str:
            """Stub for eval — no memory backend available.

            Args:
                query: Search query (ignored in eval).
                role: Memory role (ignored in eval).
                top_k: Max results (ignored in eval).
            """
            return "(eval harness: memory disabled — proceed with grep_repos/read_file)"
        return search_memory

    def _make_noop_lookup_repo(ctx: dict):
        from smolagents import tool

        @tool
        def lookup_repo(name: str) -> str:
            """Stub for eval — repo catalog not available.

            Args:
                name: Repo directory name.
            """
            project = fixture.get("project") or name
            return (
                f"Repo: {project} (java)\n"
                f"Path: {NUC_REPO_BASE}/{project}\n"
                f"Stack: spring-boot, java\n"
                f"Entry: mvn spring-boot:run\n"
                f"Compile gate: mvn -DskipTests compile\n"
                f"Ports: 8090\n"
                f"Dockerfile: yes\n"
                f"Overview: PosClientBackend Spring Boot client API."
            )
        return lookup_repo

    # Patch in our locals before make_tools runs.
    planner_tools.make_write_plan = _make_write_plan_local
    planner_tools.make_create_child_ticket = _make_create_child_ticket_local
    planner_tools.make_search_memory = _make_noop_search_memory
    planner_tools.make_lookup_repo = _make_noop_lookup_repo

    # Build a fake LLMConfig. The MLX server at port 1235 receives the bare model
    # path as the OpenAI 'model' field (mlx_lm rejects 'openai/Users/...' as a HF
    # repo id). LiteLLM uses the leading 'openai/' for provider routing only —
    # the path that follows must keep its leading slash so the server sees the
    # raw filesystem path. So: "openai/" + "/Users/..." = "openai//Users/...".
    _raw_model = _os.environ["AIFORGE_PLANNER_MODEL"]
    if _raw_model.startswith("openai/"):
        _model = _raw_model
    else:
        _model = f"openai/{_raw_model}"  # absolute path keeps its leading slash
    class _Cfg:
        base_url = _os.environ["AIFORGE_PLANNER_BASE_URL"]
        model = _model
        api_key = _os.environ["AIFORGE_PLANNER_API_KEY"]

    # Block the agent_config preference path — we want our env vars to win.
    # build_planner_agent catches *any* Exception in the agent_config path
    # and falls back to llm_config. Inject a stub that raises on resolve.
    import types as _types
    _stub = _types.ModuleType("aiforge_core.runtime.agent_config")
    def _raise_resolve(role):  # noqa: ARG001
        raise RuntimeError("eval harness: agent_config disabled")
    _stub.resolve_litellm = _raise_resolve  # type: ignore[attr-defined]
    import sys as _sys
    _sys.modules["aiforge_core.runtime.agent_config"] = _stub

    ticket = _FakeTicket(
        id=999000 + int(time.time()) % 1000,
        identifier=run_id.upper(),
        title=fixture["title"],
        body=fixture["body"],
        project=fixture["project"],
    )

    context_bundle = (
        f"Project: {ticket.project}\n"
        f"Title: {ticket.title}\n"
        f"Worktree root for grep/list/read: {worktree}\n"
    )

    t0 = time.time()
    tool_dist: dict[str, int] = {}
    final_text = ""
    error = None

    try:
        # Override WORKTREE_ROOT for the planner tool ctx — we want it to look at
        # the worktree we just made (so file paths resolve against the right tree).
        from aiforge_core.runtime import config as _cfg_mod
        _cfg_mod.WORKTREE_ROOT = worktree  # type: ignore[attr-defined]
        agent, task_prompt = planner_agent.build_planner_agent(ticket, context_bundle, _Cfg())
        result = agent.run(task=task_prompt)
        final_text = str(result) if result is not None else ""
        # Pull tool dist from agent step memory if available.
        try:
            for step in getattr(agent.memory, "steps", []) or []:
                tcalls = getattr(step, "tool_calls", None) or []
                for tc in tcalls:
                    name = getattr(tc, "name", None) or (
                        tc.get("name") if isinstance(tc, dict) else None
                    )
                    if name:
                        tool_dist[name] = tool_dist.get(name, 0) + 1
        except Exception:
            pass
        turns = getattr(agent, "step_number", 0)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        turns = 0
    wall = time.time() - t0

    plan_text = captured_plan.get("text") or final_text
    return {
        "backend": "smolagents_codeagent",
        "wall_clock_s": round(wall, 1),
        "turns": turns,
        "tool_call_distribution": tool_dist,
        "tool_use_count": sum(tool_dist.values()),
        "plan_text": plan_text,
        "plan_files": captured_plan.get("files") or [],
        "subticket_count": len(subticket_log),
        "subtickets": subticket_log,
        "final_summary": final_text[:1500],
        "error": error,
        "wrote_plan": bool(captured_plan.get("text")),
    }


# ─────────────────────────────────────────────────────────────────────────
# Backend 2: GenericAgent text-protocol
# ─────────────────────────────────────────────────────────────────────────
_GA_PLANNER_PREAMBLE = """You are the AIForge Planner agent operating through GenericAgent.

You DO NOT write production code. You produce a plan that the Doer will follow.

You MUST produce a Markdown plan and write it to `{plan_path}` via the `file_write` tool.

The plan MUST contain ALL of these sections, in this order, using these exact h2 headings:

## Files
- one repo-relative file path per bullet, each one you'll read first to confirm it exists

## Plan
1. numbered high-level steps the Doer will execute (no actual code blocks)
2. ...

## Signatures
file_relative_path:line: <verified method signature>
... one per line, pulled by reading the file

## Pitfalls
short notes on compile gotchas the Doer should avoid

Hard rules:
- ALL file_read / list_dir / grep / file_write paths MUST be ABSOLUTE paths starting with
  `{worktree}/...`. Relative paths are evaluated against the GenericAgent install dir, NOT
  the worktree, so they will NOT find your files.
- BEFORE writing the plan, use `file_read` (and optionally `grep` / `list_dir`) on every
  file you list under ## Files. If the file does not exist, REMOVE it from your ## Files
  list. The repo-relative path you list under ## Files should drop the absolute prefix.
- DO NOT call `file_patch`. DO NOT modify any source file. The plan is your only output.
- DO NOT call `code_run` for compilation — the Doer compiles, not you.
- Budget: at most 12 turns. After writing `{plan_path}`, reply with a single short summary
  and stop calling tools.

Working directory: `{worktree}`
"""


def _ssh_chain(cmd: str, *, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a shell command on NUC via the MS jump host.

    The naive `f"ssh nuc {json.dumps(cmd)}"` form runs the inner cmd through
    *two* shells; `$!`, `$()` and friends get expanded on the outer shell
    first and arrive empty at the inner shell. Pipe the cmd over stdin
    instead — the outer ssh forwards bytes verbatim.
    """
    full = ["ssh", "manikanta@192.168.70.185",
            "ssh mani@10.10.10.2 'bash -s'"]
    return subprocess.run(full, input=cmd, capture_output=True, text=True,
                          timeout=timeout, check=False)


def _make_worktree_nuc(project: str, run_id: str) -> str:
    repo = f"{NUC_REPO_BASE}/{project}"
    worktree = f"/tmp/planner-eval/{run_id}"
    branch = f"planner-eval/{run_id}"
    cmd = (
        f"set -e; mkdir -p /tmp/planner-eval; "
        f"cd {repo} && git fetch -q origin 2>&1 | tail -3 && "
        f"git worktree add -B {branch} {worktree} origin/master 2>&1 | tail -3 && "
        f"echo WT_OK"
    )
    cp = _ssh_chain(cmd, timeout=120)
    if "WT_OK" not in cp.stdout:
        raise RuntimeError(f"worktree create failed: {cp.stdout}\n{cp.stderr}")
    return worktree


def _cleanup_worktree_nuc(project: str, run_id: str, worktree: str) -> None:
    repo = f"{NUC_REPO_BASE}/{project}"
    _ssh_chain(
        f"cd {repo} && git worktree remove --force {worktree} 2>&1 | tail -3 && "
        f"git branch -D planner-eval/{run_id} 2>&1 | tail -1",
        timeout=60,
    )


def _run_ga_planner_nuc(fixture: dict, run_id: str, worktree: str,
                       timeout_s: int) -> dict:
    """Launch GA on NUC with --llm_no 1 (planner config) and a planner preamble.

    GA reads input.txt, writes output.txt with [ROUND END] sentinel when done."""
    plan_path = f"{worktree}/plan.md"
    preamble = _GA_PLANNER_PREAMBLE.format(plan_path=plan_path, worktree=worktree)
    body = fixture["body"]
    title = fixture["title"]

    user_prompt = (
        f"Working directory: `{worktree}` — every shell or file_read must be inside.\n\n"
        f"# Ticket: {title}\n\n{body}\n\n"
        f"Write the plan to `{plan_path}` using `file_write`. Stop after writing it."
    )
    full_input = preamble + "\n\n" + user_prompt

    task_dir = f"{GA_DIR}/temp/{run_id}"
    output_path = f"{task_dir}/output.txt"
    pid_path = f"{task_dir}/agentmain.pid"
    log_path = f"{task_dir}/agentmain.log"

    # Use heredoc via a temp file to avoid quoting hell. Build the prompt
    # locally, scp it over, then run agentmain.
    setup = (
        f"mkdir -p {task_dir} && rm -f {output_path} {pid_path} {log_path}"
    )
    cp = _ssh_chain(setup, timeout=15)
    if cp.returncode != 0:
        raise RuntimeError(f"task_dir setup failed: {cp.stderr}")

    # Write the prompt to NUC via stdin → bash heredoc on the inner shell.
    # We can't reuse _ssh_chain because we need to send the prompt as data,
    # not as a shell command.
    delim = f"EVAL1B_{run_id.replace('-', '_').upper()}_END"
    pipe_script = (
        f"cat > {task_dir}/input.txt <<'{delim}'\n"
        + full_input
        + f"\n{delim}\n"
    )
    write_cmd = ["ssh", "manikanta@192.168.70.185",
                 "ssh mani@10.10.10.2 'bash -s'"]
    proc = subprocess.run(write_cmd, input=pipe_script, text=True,
                          capture_output=True, timeout=30, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"prompt write failed: {proc.stderr}")

    # Launch agentmain.py --llm_no 1 (planner config). The double-ssh pipe
    # holds open until the spawned process closes ALL inherited fds; setsid
    # alone is not enough on debian default sshd. Use the at(1)-style trick:
    # write a tiny launcher script and run it via setsid in its own session,
    # then `exit 0` from the bash -s parent so the outer ssh sees EOF.
    launcher = f"{task_dir}/launch.sh"
    launch_script = (
        f"cat > {launcher} <<'LAUNCH_EOF'\n"
        f"#!/bin/bash\n"
        f"cd {GA_DIR}\n"
        f"GA_LANG=en exec python3 agentmain.py --task {run_id} --llm_no 1\n"
        f"LAUNCH_EOF\n"
        f"chmod +x {launcher}\n"
        f"setsid bash -c '{launcher} > {log_path} 2>&1 < /dev/null & echo $! > {pid_path}; disown' < /dev/null > /dev/null 2>&1 &\n"
        f"sleep 1.5\n"
        f"cat {pid_path}\n"
        f"exit 0\n"
    )
    cp = _ssh_chain(launch_script, timeout=30)
    pid_str = (cp.stdout.strip().splitlines() or [""])[-1]
    if not pid_str.isdigit():
        raise RuntimeError(
            f"agentmain launch failed: stdout={cp.stdout!r} stderr={cp.stderr!r}"
        )
    print(f"  GA pid={pid_str}, polling for [ROUND END]")

    t0 = time.time()
    deadline = t0 + timeout_s
    seen = False
    while time.time() < deadline:
        time.sleep(8)
        check = _ssh_chain(
            f"test -f {output_path} && grep -q '\\[ROUND END\\]' {output_path} "
            f"&& echo DONE || echo PENDING",
            timeout=15,
        )
        if "DONE" in check.stdout:
            seen = True
            break
    wall = time.time() - t0

    # Kill the process so it doesn't hang on reply.txt.
    _ssh_chain(
        f"kill {pid_str} 2>/dev/null; sleep 1; "
        f"kill -9 {pid_str} 2>/dev/null || true",
        timeout=15,
    )

    # Fetch output + log + plan.md.
    out_text = _ssh_chain(f"cat {output_path} 2>/dev/null || true", timeout=30).stdout
    log_tail = _ssh_chain(f"tail -200 {log_path} 2>/dev/null || true", timeout=30).stdout
    plan_text_cp = _ssh_chain(f"cat {plan_path} 2>/dev/null || true", timeout=30)
    plan_text = plan_text_cp.stdout

    # Parse tool dist + turn count from output.txt.
    tool_marker = re.compile(r"🛠️\s*Tool:\s*`([a-z_]+)`")
    tool_compact = re.compile(r"🛠️\s+([a-z_]+)\(")
    turn_re = re.compile(r"LLM Running \(Turn (\d+)\)")
    tool_dist: dict[str, int] = {}
    for m in tool_marker.finditer(out_text):
        n = m.group(1)
        tool_dist[n] = tool_dist.get(n, 0) + 1
    if not tool_dist:
        for m in tool_compact.finditer(out_text):
            n = m.group(1)
            tool_dist[n] = tool_dist.get(n, 0) + 1
    turns = 0
    for m in turn_re.finditer(out_text):
        turns = max(turns, int(m.group(1)))

    return {
        "backend": "genericagent_text_protocol",
        "wall_clock_s": round(wall, 1),
        "turns": turns,
        "tool_call_distribution": tool_dist,
        "tool_use_count": sum(tool_dist.values()),
        "plan_text": plan_text,
        "wrote_plan": bool(plan_text.strip()),
        "round_end_seen": seen,
        "output_len": len(out_text),
        "log_tail": log_tail[-3000:],
        "ga_summary_tail": out_text[-3000:],
    }


# ─────────────────────────────────────────────────────────────────────────
# Token estimation (char/4 fallback when no usage info)
# ─────────────────────────────────────────────────────────────────────────
def _estimate_tokens(text: str) -> int:
    return len(text or "") // 4


# ─────────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────────
def run_one(backend: str, fixture: dict, seq: int, *, timeout_s: int) -> dict:
    fid = fixture["id"]
    project = fixture["project"]
    run_id = f"{fid}-{seq:02d}-{uuid.uuid4().hex[:6]}-{backend[:4]}"
    out_dir = RESULTS_DIR / backend / fid
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[EVAL-1b backend={backend} fixture={fid} seq={seq}] run_id={run_id}")

    worktree = _make_worktree_nuc(project, run_id)
    print(f"  worktree={worktree}")
    try:
        if backend == "smolagents_codeagent":
            result = _run_smolagents_planner(fixture, run_id, worktree, out_dir)
        elif backend == "genericagent_text_protocol":
            result = _run_ga_planner_nuc(fixture, run_id, worktree, timeout_s)
        else:
            raise ValueError(f"unknown backend: {backend}")
    finally:
        _cleanup_worktree_nuc(project, run_id, worktree)

    quality = _score_plan(result.get("plan_text") or "", fixture)
    plan_text = result.get("plan_text") or ""
    estimated_tokens = _estimate_tokens(plan_text)

    metrics = {
        "wall_clock_s": result["wall_clock_s"],
        "turns": result.get("turns", 0),
        "tool_use_count": result.get("tool_use_count", 0),
        "tool_call_distribution": result.get("tool_call_distribution", {}),
        "plan_quality": quality,
        "plan_chars": len(plan_text),
        "plan_tokens_estimated": estimated_tokens,
        "wrote_plan": bool(result.get("wrote_plan")),
        "subticket_count": result.get("subticket_count", 0),
        "pass": (
            bool(result.get("wrote_plan"))
            and quality["all_target_files_mentioned"]
            and quality["quality_score"] >= 0.5
        ),
        "error": result.get("error"),
    }
    payload = {
        "track": "EVAL-1b",
        "backend": backend,
        "fixture_id": fid,
        "fixture_name": fixture.get("name", ""),
        "seq": seq,
        "run_id": run_id,
        "metrics": metrics,
        "plan_excerpt": plan_text[:1200],
        "tail": (result.get("ga_summary_tail") or result.get("final_summary") or "")[-1500:],
    }
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"{seq:02d}_{ts}.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"  → {out_path}")
    print(f"  pass={metrics['pass']} wall={metrics['wall_clock_s']}s "
          f"turns={metrics['turns']} tools={metrics['tool_use_count']} "
          f"quality={quality['quality_score']} files_ok={quality['all_target_files_mentioned']}")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True,
                    choices=["smolagents_codeagent", "genericagent_text_protocol", "both"])
    ap.add_argument("--fixtures", default="F1,F3,F5")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    fids = [x.strip() for x in args.fixtures.split(",") if x.strip()]
    fixtures = [_load_fixture(f) for f in fids]
    backends = (
        ["smolagents_codeagent", "genericagent_text_protocol"]
        if args.backend == "both" else [args.backend]
    )
    print(f"=== EVAL-1b backends={backends} fixtures={fids} samples={args.samples} ===")
    for backend in backends:
        for fixture in fixtures:
            for seq in range(1, args.samples + 1):
                try:
                    run_one(backend, fixture, seq, timeout_s=args.timeout)
                except KeyboardInterrupt:
                    sys.exit("interrupted")
                except Exception as exc:
                    print(f"  ERROR backend={backend} fixture={fixture['id']} seq={seq}: {exc}")


if __name__ == "__main__":
    main()
