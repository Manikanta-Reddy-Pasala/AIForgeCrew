# aiforge_core.agents — v5 production pipeline

Six archetypes, executed by `runtime.adk_runner` as an ADK
`SequentialAgent`:

```
architect (external)
        │
        ▼
   planner ──► verifier ──► LoopAgent ─► doer ─► feedback ──► learner
                 │           (loop until verdict ∈ {pass, fail, scope_violation})
                 └─► reject → re-plan (cap 3)
```

| Archetype | Runtime | Role |
|-----------|---------|------|
| **architect** | external Claude Code (human-driven) | Writes parent ticket; never edits code |
| **planner**   | ADK + GenericAgent text-protocol      | Reads parent ticket → emits plan + child subtickets + scope allowlist |
| **verifier**  | ADK direct LiteLLM (single completion) | Pre-execution plan critic. Reject → re-plan with issues folded in |
| **doer**      | ADK + GenericAgent text-protocol      | Edits files inside the subticket allowlist; runs compile + tests |
| **feedback**  | ADK direct LiteLLM (single completion) | Post-execution judge. Emits JSON `{verdict, rationale}` |
| **learner**   | ADK direct LiteLLM (single completion) | Runs only on `verdict=pass`. Writes :Fact rows to memory |

Source of truth for tools, max_turns, memory scope, and termination
contract: [`agents.yaml`](agents.yaml).

## Provider routing

Per-archetype provider + model is configurable at runtime via
`~/.aiforge/agent_config.json` (see `aiforge_core.config.agent_config`).
Bulk presets:

```bash
aiforge-profile apply claude_local   # all 6 → claude-opus-4-7 (subscription CLI)
aiforge-profile apply ollama_cloud   # all 6 → qwen3-coder:480b
aiforge-profile apply local          # all 6 → LM Studio MLX

aiforge-profile set architect claude_local claude-opus-4-7
aiforge-profile set doer       ollama_cloud qwen3-coder:480b
```

Settings UI exposes the same surface at `/api/agents/v2/*`.

## Files

| Path | Role |
|------|------|
| `agents.yaml`         | per-agent contract (tools, max_turns, memory scope) |
| `loader.py`           | reads + validates `agents.yaml` |
| `registry.py`         | `registry.build(name)` — only consumer of archetype classes |
| `base.py`             | `BaseArchetype` — provider + model + tool plumbing |
| `architect.py` · `planner.py` · `verifier.py` · `doer.py` · `learner.py` | archetype implementations |
| `defaults.py` · `config.py` | per-archetype default sampling params |
| `docs/`               | per-archetype design notes |

`feedback` has no archetype class — it's a single-completion judge wired
directly into the ADK LoopAgent, see `runtime.adk_runner`.
