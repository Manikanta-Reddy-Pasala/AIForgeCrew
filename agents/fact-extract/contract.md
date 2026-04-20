# Fact Extract — Contract

## Inputs (prepared by orchestrator, not tool calls)
- Parent + children ticket bodies
- All T1 rows for parent
- Precomputed diff summary across merged children

## Outputs
- XML reflection block → parsed into memory_proposals (T2 facts, T3 recipes)
