# Hardware Guide

> Phase P0 gate. Target: run 3 concurrent local agents (Tester + Sr Dev + Sr Architect) with acceptable TTFT.

## Reference Build
- Apple M3 Ultra, 192 GB unified memory, 60-core GPU
- 2 TB internal NVMe (models + vector stores)
- 10 GbE for repo pulls

## Why M3 Ultra
- Unified memory keeps 32B-class quantized models + embeddings resident
- MPS / Metal backends for llama.cpp / Ollama give acceptable tokens/s

## Sizing Rules of Thumb

| Quant | 13B | 34B | 70B |
|-------|----:|----:|----:|
| Q4_K_M | ~8 GB  | ~20 GB | ~40 GB |
| Q5_K_M | ~10 GB | ~24 GB | ~48 GB |

Budget 1.5× model size for KV-cache headroom per concurrent agent.

## Pre-P0 checklist
- [ ] Disk free ≥ 500 GB
- [ ] `sudo spctl --global-disable` not required — models run in user space
- [ ] `ulimit -n 65535`
