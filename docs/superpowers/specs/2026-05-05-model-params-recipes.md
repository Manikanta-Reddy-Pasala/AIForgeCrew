# Per-model coding sampler recipes

Bundled `agents.defaults.yaml` is tuned for **Qwen3-Coder-Next** (the default
local Doer/Planner). Switching models means switching samplers — different
families need very different hyper-params for code generation.

## Qwen3-Coder-Next 80B (default, baked into agents.defaults.yaml)

Per Qwen team HuggingFace card, code-mode sampler:

```yaml
temperature: 0.7
top_p: 0.8
top_k: 20
presence_penalty: 1.5
repetition_penalty: 1.05
```

**Empirical finding from this repo's benchmarks:** dropping temperature
to 0.2 makes Qwen3-Coder fall into "read-everything-first" mode and never
emit `action: edit` steps → Doer skipped. The 0.7/1.5 combo is required
for the model to actually plan modifications.

## Granite-4.1 30B (alternative local Doer)

Per IBM Granite team recommendations for code:

```yaml
temperature: 0.2
top_p: 0.95
top_k: 50
presence_penalty: 0.0
repetition_penalty: 1.05
```

Granite is **less creative** than Qwen3-Coder. Lower temp + higher top_p
yields cleaner output. Setting Qwen-style 0.7/1.5 on Granite makes it
emit malformed Java (statements at class level, broken paren matching).

## Cloud Qwen3-Coder 480B (Ollama Cloud)

Same family as Qwen3-Coder-Next — use the Qwen recipe above. Network
latency is the bottleneck, not sampler tuning.

## Cloud Claude (subscription via `claude_local` provider)

CLI exposes no sampler — set via the Claude subscription dashboard if needed.
Default behaviour is fine for most code tasks.

---

## How to override per-model

When swapping the Doer model, place these per-archetype params into
`~/.aiforge/agents.yaml` (global, picked up by every run on the host):

### Granite override example

```yaml
archetypes:
  doer:
    model: granite-4.1-30b-mxfp8
    temperature: 0.2
    top_p: 0.95
    top_k: 50
    presence_penalty: 0.0
    repetition_penalty: 1.05
  planner:
    model: granite-4.1-30b-mxfp8
    temperature: 0.2
    top_p: 0.95
    top_k: 50
    presence_penalty: 0.0
    repetition_penalty: 1.05
```

### Env-var per-call override (one-shot)

```bash
AIFORGE_DOER_MODEL=granite-4.1-30b-mxfp8 \
AIFORGE_DOER_TEMPERATURE=0.2 \
AIFORGE_DOER_TOP_P=0.95 \
AIFORGE_DOER_PRESENCE_PENALTY=0 \
AIFORGE_DOER_REPETITION_PENALTY=1.05 \
aiforge-agents-run --repo PosClientBackend --title "..." --body "..."
```

## Empirical baseline (all benchmarked on PosClientBackend ticket flow)

| Provider × params | Plan steps | Doer ran | Files edited |
|---|---|---|---|
| Qwen3-Coder-Next @ 0.2/0.95 (wrong) | 2 read | ❌ | 0/1 |
| **Qwen3-Coder-Next @ 0.7/0.8/0.5/1.5 (official)** | **5 (3 edit)** | **✅ 267s** | **3/1** |
| Granite-4.1 30B @ 0.3/0.85 (wrong-ish) | 6 | ✅ 158s | 3/3 (broken Java) |
| Granite-4.1 30B @ 0.2/0.95 (official, predicted) | TBD | TBD | TBD — re-run pending |
| Cloud Qwen3-Coder 480B @ defaults | 5 | ✅ 388s | 3/10 |

The Granite-official-params row is open: bench needs re-run with proper
params to validate IBM's recommendation against this repo's tickets.
