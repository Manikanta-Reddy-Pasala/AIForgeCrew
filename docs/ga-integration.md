# GenericAgent integration

How AIForgeCrew embeds GenericAgent (GA) and how to upgrade it without
breaking production.

## What we use GA for

| Role | Uses GA? | Why |
|------|----------|-----|
| Architect | no | external (Claude Code) |
| **Planner** | NO — direct LiteLLM | text-protocol Chinese boilerplate adds 3KB/turn for a role that just emits markdown. Bypassed. See `aiforge_core/planner/ga_runner.py`. |
| **Doer** | YES — full GA loop | text-protocol session sidesteps mlx_lm 0.31 native `tool_calls` serialization bug. See `aiforge_core/doer/ga_runner.py` + `ga_compat.py`. |
| Feedback | no | direct LiteLLM single-shot |
| Learner | no | direct LiteLLM single-shot, writes `:Fact` via Neo4j plugin |

Only one component (Doer) is GA-coupled. That's the upgrade surface.

## Single import seam

ALL Doer code that touches GA imports from
`aiforge_core/doer/ga_compat.py` — never from upstream GA directly.

```python
from aiforge_core.doer.ga_compat import (
    GA_COMPAT_VERSION, GA_OVERRIDE_POINTS, ParentShim,
    ga_dir, ga_sha, import_ga, load_tools_schema,
)
```

When GA upgrades and an internal symbol moves, edit ONLY `ga_compat.py`
and bump `GA_COMPAT_VERSION`. Run F-suite. Ship.

## What we override on GA's `GenericAgentHandler`

Tracked in `ga_compat.GA_OVERRIDE_POINTS`:

| Method | Visibility | Why we override |
|--------|------------|-----------------|
| `_get_abs_path` | private (underscore) | ScopeGuard chokepoint — every path-bearing tool resolves through here |
| `tool_before_callback` | public protocol | Reject forbidden tools (`ask_user`, `start_long_term_update`, web tools) |
| `do_file_patch` | public tool method | Increment `edit_block_ok` counter |
| `do_file_write` | public tool method | Increment `edit_block_ok` counter |
| `do_code_run` | public tool method | Detect `mvn` BUILD SUCCESS / FAILURE, update counters |
| `turn_end_callback` | public protocol | Reserved for Neo4j `:Turn` mirror (currently lifted to ADK plugin) |

**Risk zone**: `_get_abs_path` is private. If GA renames it on a major
upgrade, we lose the ScopeGuard chokepoint. Action item: when next
GA version drops, audit whether the rename happened and consider
moving ScopeGuard checks into `tool_before_callback` instead.

## Other GA contract dependencies

```
GA dir layout                    What we read
─────────────────────────────    ──────────────────────────────────────
agent_loop.py                    agent_runner_loop, exhaust, StepOutcome
ga.py                            GenericAgentHandler base class
llmcore.py                       LLMSession, ToolClient
assets/tools_schema.json         9-tool OpenAI-function-style list
mykey.py                         Substring-matched config dict names —
                                 we use `oai_*_config` (no `native_`)
                                 to force text-protocol session
```

## Pin protocol

We pin GA at a known git SHA in `.aiforge/ga-version.lock` (project
root). The lock is committed to git so every checkout knows which GA
revision it was tested against.

```bash
# Show current pin + drift status
./scripts/runtime/ga-pin.sh --show

# Pin live GA HEAD into the lock (records SHA + date + user)
./scripts/runtime/ga-pin.sh

# Smoke gate — exit 1 if live GA != lock (run before ticket processing)
./scripts/runtime/ga-pin.sh --check
```

Current pin: `.aiforge/ga-version.lock` carries the SHA + date + the
user who pinned it. Update the pin only after the smoke gate passes.

## Smoke gate (F-suite)

The eval fixture suite is the regression cliff. Every GA upgrade MUST
re-run the chain:

```bash
.venv/bin/python scripts/evals/run_genericagent_eval.py \
    --chain F7a,F7b,F7c
```

Pass criteria:
- All 3 subtickets `task_pass=True`
- `compile_pass=True` for each
- `expected_files_present=True` for each
- No `rule_violations` in metrics

Fail = adapter fix needed in `ga_compat.py` BEFORE shipping new GA SHA.

## Upgrade checklist

When upstream GA cuts a new release (or you `git pull` the GA dir):

1. `./scripts/runtime/ga-pin.sh --show` — confirm drift detected
2. `python -c "from aiforge_core.doer.ga_compat import import_ga; import_ga()"` — if it raises, fix `ga_compat.py`
3. Walk every name in `GA_OVERRIDE_POINTS` — confirm signatures match
4. Audit `ParentShim.__slots__` against GA's handler reads
5. Audit `assets/tools_schema.json` schema shape (still OpenAI-function-style?)
6. Run F-suite chain (above)
7. If green: `./scripts/runtime/ga-pin.sh` to update lock + commit
8. If red: fix `ga_compat.py`, bump `GA_COMPAT_VERSION` minor, re-run

## What you do NOT have to touch on a GA upgrade (if compat layer holds)

- `aiforge_core/doer/ga_runner.py` (uses ga_compat exclusively)
- `aiforge_core/doer/orchestrator_bridge.py` (backend dispatch only)
- `aiforge_core/agents.yaml` (tools.allowed list)
- `aiforge_core/runtime/adk_workflow.py` (ADK-side wiring)

If any of those need changes, the compat layer didn't insulate. Fix
the compat layer instead.

## Worst-case fallback

`AIFORGE_DOER_BACKEND=toolcalling` flips Doer back to the legacy
smolagents path. Documented in `agents.yaml` as the fallback. Won't
recover from the mlx_lm tool_calls bug, but ships if GA migration
turns into a fire drill.

## Future work

- **Vendor GA at SHA via git submodule** — currently rsync'd to NUC
  from MS. A submodule pinned to the same SHA as `.aiforge/ga-version.lock`
  would make the integration reproducible from a fresh checkout
  without the SSH chain.
- **Move ScopeGuard off `_get_abs_path`** — use `tool_before_callback`
  exclusively. Eliminates the only private-API override.
- **Push GA to PyPI upstream** — sponsor a release. Then pin in
  pyproject.toml instead of git SHA, drop the rsync step.
