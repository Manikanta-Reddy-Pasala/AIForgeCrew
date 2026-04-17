# Model Evaluation Matrix

> Phase P9 deliverable. This file tracks candidate models per role.

## Roles + criteria

| Role | Primary axis | Secondary |
|------|--------------|-----------|
| EM (cloud) | planning quality, ambiguity detection | cost/1k tokens |
| Tester | test coverage breadth, boundary-case recall | speed |
| Sr Developer | code correctness at first pass | context window |
| Sr Architect | review precision, security reasoning | hallucination rate |

## Candidates (fill in P9)

| Role | Candidate | Params | Quant | TTFT (ms) | Pass@1 | Notes |
|------|-----------|--------|-------|-----------|--------|-------|
| EM (cloud) | TBD | — | — | — | — | — |
| Tester | TBD | — | — | — | — | — |
| Sr Dev | TBD | — | — | — | — | — |
| Sr Arch | TBD | — | — | — | — | — |

## Harness
- Tests: private eval set of 30 tickets from this repo's backlog
- Metrics: plan quality, test coverage, code pass@1, review precision
- Command: `scripts/evaluate-models.sh --role <role>` (P9)
