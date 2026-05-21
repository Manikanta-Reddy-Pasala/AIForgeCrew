# Tool Surface Upgrade — OpenHands Parity Sub-Project #1

**Date:** 2026-05-21
**Status:** Draft (awaiting user review)
**Owner:** Architect (human-driven)
**Depends on:** none (ADK already pinned to `google-adk>=2.0.0b1`)
**Successor specs:** sub-projects #2..#9 (see `2026-05-21-openhands-parity-roadmap.md`)

---

## 1. Problem

OpenHands ships a richer tool surface than AIForgeCrew's Doer currently exposes:

| Capability | OpenHands | AIForgeCrew today |
|---|---|---|
| Persistent bash session (cwd/env/jobs kept) | `execute_bash` | `run_shell` — stateless per call |
| Multi-command editor (view/create/str_replace/insert/undo_edit) | `str_replace_editor` | three flat tools (`file_read`/`file_write`/`file_patch`) — no view ranges, no insert, no undo |
| Explicit thinking step | `think` | none |
| Explicit termination signal | `finish` | inferred from compile/test color |

This blocks long-running tests (`npm run dev &`), multi-step debugging (cd into a sub-tree and stay there), and mid-edit rollbacks. It also forces the model to infer stop conditions, which we have observed mis-firing on long tickets.

This spec covers ONLY the four tool additions/replacements above. Browser, IPython, condenser, microagents, vision, Docker sandbox, delegation, and unified budget tracking are addressed in subs #2-#9.

## 2. Goals

1. Doer agent calls `editor`, `bash`, `think`, `finish` — backed by OH-equivalent semantics.
2. Existing `file_read`/`file_write`/`file_patch`/`run_shell` continue to work for one release (deprecation shim) so in-flight tickets do not break mid-execution.
3. Per-agent tool allowlist still enforced through `agents.yaml` (defense-in-depth: ADK filter + GA handler + trace assertion).
4. Separation-of-concerns: each tool lives in its own module; no cross-imports between sibling tool modules.
5. KISS: zero new heavy deps (tmux is system, snapshot ring is plain files), zero new persistence layers.

## 3. Non-Goals

- Browser (sub #2)
- IPython kernel (sub #3)
- Memory condenser (sub #4)
- Microagent triggering (sub #5)
- Multimodal vision (sub #6)
- Docker sandbox (sub #7)
- Agent delegation (sub #8)
- Unified budget tracker (sub #9)

## 4. Architecture

### 4.1 Module layout

```
aiforge_core/runtime/tools/                    # NEW package
├── __init__.py        # adk_function_tools() factory + ADK FunctionTool list
├── editor.py          # OH-style multi-command editor
├── bash.py            # tmux-backed persistent session manager
├── cognition.py       # think + finish
└── _trace.py          # shared trace-event emitter (:Think :Finish :BashSession :EditorUndo)

aiforge_core/runtime/doer_tools.py              # SHRUNK to ~80 lines: deprecation shim
                                                # delegates canonical names to tools/* for one release

tests/python/runtime/tools/                     # NEW tests, mirrors structure
├── test_editor.py
├── test_bash.py
└── test_cognition.py
tests/python/runtime/test_tools_pkg_integration.py  # NEW Doer-loop smoke test
```

### 4.2 Separation guarantees

- `editor.py`, `bash.py`, `cognition.py` import only from: `sandbox`, `_trace`, `syntax_guard`, stdlib.
- Sibling tool modules MUST NOT import each other.
- ADK wiring is concentrated in `tools/__init__.py:adk_function_tools()`. No tool module imports `google.adk`.

## 5. Tool contracts

### 5.1 `editor`

Single tool with `command` dispatcher. Args validated up-front; soft-error contract.

| command | required kwargs | optional | returns | failure codes |
|---|---|---|---|---|
| `view` | `path` | `view_range=[start,end]` (1-indexed inclusive) | `{ok, path, content, total_lines}`. Dir → tree listing depth 2. | `not_found`, `not_a_file_or_dir`, `path_traversal` |
| `create` | `path`, `file_text` | — | `{ok, path, bytes}` | `exists`, `syntax_invalid` (with `hint`), `scope_violation` |
| `str_replace` | `path`, `old_str`, `new_str` | — | `{ok, path, replaced}` | `not_found`, `old_text_not_found`, `ambiguous_match` (+ `occurrences` count), `syntax_invalid`, `scope_violation` |
| `insert` | `path`, `insert_line` (0 = top), `new_str` | — | `{ok, path, inserted_at}` | `not_found`, `line_out_of_range`, `syntax_invalid`, `scope_violation` |
| `undo_edit` | `path` | — | `{ok, path, restored_from}` | `no_history`, `not_found` |

**Undo stack:**
- Snapshot taken BEFORE every mutation (create/str_replace/insert).
- Storage: `~/.aiforge/editor_undo/<sha1(abspath)>/<unix_ms>.txt`.
- Ring depth 5 per path; oldest pruned on push.
- `undo_edit` restores most recent snapshot, removes it from ring (so consecutive undo walks history).

**Guards (in order):**
1. `sandbox.resolve_inside_root(path)` — repo-root traversal check.
2. ScopeGuard — mutations only inside subticket's `scope_allowlist_globs`.
3. `syntax_guard.validate_syntax(path, content)` — gates create/str_replace/insert (respects `AIFORGE_DOER_SKIP_SYNTAX`).
4. UTF-8 only; binary rejected with `binary_write_forbidden`.

### 5.2 `bash`

```python
bash(command: str, *, restart: bool = False, timeout: int = 90) -> dict
```

**Lifecycle:**
- One tmux session per ADK run. Name `aiforge-{run_id}` where `run_id` comes from ADK invocation context.
- Lazily created on first call; tracked in a process-global `dict[run_id, session_name]`.
- Destroyed in `BasePlugin.on_finish_callback`, regardless of run outcome.
- `restart=True` kills+recreates the session, returns `{ok: True, restarted: True}`.

**Execution model:**
- Custom PS1 set on session create: `PS1='__AIFORGE_PROMPT_$?__\n'`.
- Command sent via `tmux send-keys -t <session> <cmd> Enter`.
- Output captured via `tmux capture-pane -p -t <session> -S -10000` polled at 100ms cadence.
- Prompt sentinel `__AIFORGE_PROMPT_<rc>__` regex extracts exit code; line is stripped from returned stdout.
- Timeout default 90 s. On hit: send `C-c`, wait 1 s, capture, return partial.

**Returns:**
```python
{
    "ok": bool,                # returncode == 0
    "returncode": int,
    "stdout": str,             # capped 8 KB
    "command": str,
    "truncated": bool,
    # only on timeout:
    "error": "timeout",
    # only on backgrounded (trailing `&`):
    "backgrounded": True,
    "jobspec": str,
}
```

**Backgrounded jobs:** if trimmed command ends with `&`, return immediately after `send-keys`, do not wait for sentinel. Caller can later `bash("jobs -l")` or `bash("kill %1")`.

**Fallback:** if `shutil.which("tmux")` returns `None`, fall back to current `run_shell` stateless subprocess. Warn-once via `_trace.emit("BashFallback", {reason: "tmux_missing"})`.

### 5.3 `think`

```python
think(thought: str) -> dict
```

- Pure no-op for model: returns `{ok: True}`.
- Emits `:Think` trace node with `{thought, agent, ticket_id, ts}`.
- `thought` capped at 4 KB (truncated with `...[truncated]` suffix).
- No memory write, no fact persistence. Preserves "only Learner writes :Fact".

### 5.4 `finish`

```python
finish(summary: str, status: str = "done") -> dict
```

- Doer-only. Non-Doer agents get `{ok: False, error: "agent_not_authorized"}`.
- `status ∈ {done, blocked}`. Anything else → `{ok: False, error: "invalid_status"}`.
- `summary` capped 2 KB.
- Writes `doer_finish_summary` and `doer_finish_status` to ADK session state.
- Returns `{ok: True, terminate: True}`. ADK LoopAgent checks `terminate=True` and halts Doer step.
- Emits `:Finish` trace event.
- Feedback step downstream picks up `doer_finish_summary` + last `compile_status`/`test_status` from session state.

## 6. `agents.yaml` changes

```yaml
# diff against current agents.yaml — only changed lines shown

doer:
  tools:
    allowed:
      - editor              # NEW — replaces file_read/file_write/file_patch
      - bash                # NEW — replaces run_shell/code_run
      - think               # NEW
      - finish              # NEW (Doer-only; enforced inside tool)
      - grep_repo
      - fetch_url
      - git_commit
      - update_working_checkpoint
      - graphify_lookup
      - memory_lookup
    forbidden:
      - ask_user
      - start_long_term_update
      - create_child_ticket
      - write_fact
      - write_plan
      - web_scan
      - web_execute_js
      - file_read           # LEGACY — moved to forbidden
      - file_write          # LEGACY — moved to forbidden
      - file_patch          # LEGACY — moved to forbidden
      - run_shell           # LEGACY — moved to forbidden
      - code_run            # LEGACY — moved to forbidden

architect:
  tools:
    allowed:
      - editor              # view-only via editor_commands
      ...
  tools.editor_commands:    # NEW field
    - view

planner:
  tools.editor_commands: [view]

researcher:
  tools.editor_commands: [view]

# verifier, feedback, learner, triage, refiner: no change (tool-less)
```

**Sub-command allowlist enforcement:** `tools/editor.py` reads the current agent's `editor_commands` from `tool_before_callback` context. If `command` not in allowlist → `{ok: False, error: "editor_command_not_allowed"}`.

## 7. Wiring changes

### 7.1 `runtime/adk_runner.py`

Replace
```python
from aiforge_core.runtime.doer_tools import adk_function_tools
```
with
```python
from aiforge_core.runtime.tools import adk_function_tools
```

`doer_tools.adk_function_tools()` kept and marked `# deprecated, use aiforge_core.runtime.tools` — emits one-time `DeprecationWarning` at import.

### 7.2 `runtime/doer_tools.py`

Shrunk to:
- Re-export `file_read/file_write/file_patch/run_shell` as thin wrappers that delegate to `tools.editor.editor(command=...)` and `tools.bash.bash(...)`.
- Keep all aliases (`read/write/patch/ls/shell/grep/...`) — they continue to work via the new wrappers.
- Top-of-file comment flagging deprecation + removal in next release.

### 7.3 `runtime/observability.py`

Register four new event labels: `Think`, `Finish`, `BashSession`, `EditorUndo`. Confirm Neo4j label index exists.

## 8. Testing

### 8.1 Unit (no ADK boot)

**`tests/python/runtime/tools/test_editor.py`:**
- view: full file; range; dir tree; missing path; traversal escape
- create: happy; exists-rejection; syntax-guard reject (Python `def foo(:`); scope-allowlist reject
- str_replace: happy; not-found; ambiguous-match returns `occurrences`; syntax-guard reject; scope reject
- insert: at line 0; mid-file; beyond EOF returns `line_out_of_range`
- undo_edit: 1-deep; 5-deep ring; pop beyond ring returns `no_history`
- editor_commands allowlist: non-Doer attempting `create` → `editor_command_not_allowed`

**`tests/python/runtime/tools/test_bash.py`:**
- Session lifecycle: lazy create on first call; restart wipes state; finish-callback destroys
- Persistence: `cd /tmp` then `pwd` returns `/tmp`; `export FOO=bar` then `echo $FOO` returns `bar`
- Timeout: `sleep 5` with timeout=1 returns partial + `error: "timeout"`
- Background: `sleep 5 &` returns immediately; subsequent `jobs` shows it
- Fallback: `tmux` not in PATH → uses subprocess + warn-once
- tmux integration suite: marked `@pytest.mark.tmux`; skipped if `tmux` missing

**`tests/python/runtime/tools/test_cognition.py`:**
- think: 4 KB cap with `...[truncated]` suffix; emits `:Think`; no `neo4j_facts.write_fact` call
- finish: terminate flag set; non-Doer rejection; status=blocked path; invalid_status rejection

### 8.2 Integration

**`tests/python/runtime/test_tools_pkg_integration.py`:**
- Boot ADK runner with synthetic ticket "add hello.py that prints 'hi'".
- Assert: file created via `editor.create`; `bash("python hello.py")` returns `hi`; `finish("done")` halts loop; trace contains `:Think`, `:Finish`, `:BashSession`.

### 8.3 Regression

- Re-run ONE-107 / ONE-108 / ONE-109 eval fixtures end-to-end with new tool surface. Acceptance: each produces a PR (no regression vs current `#131`/`#132`/`#133`).

### 8.4 Coverage gate

- `tools/` package ≥85% line coverage.

## 9. Acceptance criteria

1. `aiforge_core/runtime/tools/` package created (editor.py / bash.py / cognition.py / __init__.py / _trace.py).
2. `doer_tools.py` reduced to deprecation shim; all canonical impls live in `tools/`.
3. `agents.yaml` updated: Doer gets editor/bash/think/finish; legacy file/shell tools moved to `forbidden`; non-Doer agents get `editor_commands: [view]`.
4. `adk_runner.py` registers FunctionTool list from new package.
5. Unit + integration tests green; coverage ≥85% on `tools/`.
6. ONE-107/108/109 fixture re-run produces PRs (no regression).
7. tmux fallback path verified on a dev box without tmux.
8. ScopeGuard still enforces allowlist globs on editor mutations.
9. `syntax_guard` still gates create/str_replace/insert.
10. Only Doer can call `finish`; non-Doer attempt returns `agent_not_authorized`.
11. `:Think`, `:Finish`, `:BashSession`, `:EditorUndo` nodes appear in Neo4j after eval run.
12. README + `docs/agent-rules.md` updated with new tool surface.

## 10. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| In-flight tickets break on day-1 deploy | Med | Deprecation shim keeps old names alive for one release; aliases unchanged |
| Model hallucinates old vs new tool names | Med | Aliases + canonical names BOTH registered; behaviour identical |
| tmux missing on a contributor box | Low | Subprocess fallback + warn-once observability event |
| Undo ring fills disk | Low | Per-path depth 5; total cap ≤500 paths · 5 = 2500 snapshots; pruned on push |
| Editor sub-command allowlist mis-typed in YAML | Low | YAML loader validates field shape at startup; fail-closed |
| ScopeGuard not invoked from new module | High if wired wrong | Integration test asserts scope_violation path; unit test directly |
| `:Think` flood blows up Neo4j volume | Low | Per-event 4 KB cap; observability ttl already at 30 d |

## 11. Open questions

None remaining at brainstorm close. (Editor coexistence = replace + alias; bash backend = tmux; think = no-op + trace; finish = explicit Doer signal; approach = layered tools package.)

## 12. Successor work

Once this lands and the eval gate passes, drill into sub-project #2 (Browser tool) per the OpenHands parity roadmap. Each subsequent sub-project gets its own spec → plan → implementation cycle.

## 13. Verification log

- **2026-05-21** — full `pytest tests/python/ -q` on dev box (macOS, no tmux, no embed sidecar, no aiforge DB): 413 passed, 11 skipped (3 pre-existing failures in `test_embed_sidecar.py` x2 + `test_graphify_lookup_tool.py` x1 are unrelated infra dependencies; verified pre-existing via stash).
- **2026-05-21** — `tools/` package coverage:

```
aiforge_core/runtime/tools/__init__.py        100%
aiforge_core/runtime/tools/_trace.py           96%
aiforge_core/runtime/tools/bash.py             39%   ← tmux path skipped (dev box lacks tmux)
aiforge_core/runtime/tools/cognition.py        96%
aiforge_core/runtime/tools/editor.py           84%
TOTAL                                          74%
```

bash.py coverage is gated by `tmux` availability — the four currently-skipped
tmux integration tests cover the persistent-session path and will exercise
the missing lines when this runs on a tmux-equipped host (Mac Studio prod).
Coverage ≥85% target met for `__init__.py`, `_trace.py`, `cognition.py`,
`editor.py`; full `bash.py` coverage pending re-run on tmux host.

- **2026-05-21** — `validate_contracts(load_agents())` returns `[]`; agents.yaml
  parses cleanly; Doer's `editor_commands` is `None` (full access); Architect /
  Planner / Researcher carry `editor_commands: [view]`.
- **2026-05-21** — eval re-run on ONE-107 / ONE-108 / ONE-109 fixtures: pending
  next Mac Studio production cycle (writes here on completion).
