# Architect — Contract

## Inputs
- Parent ticket body and metadata
- Retrieved context bundle from orchestrator (per-role retrieval policy)
- On review: child ticket diff + test report

## Outputs
- Planning phase: structured design comment on parent ticket
- Review phase: approve/reject comment on child ticket

## Terminal tool call
Every turn ends with:
`report(status, summary, confidence, next_action, citations[])`

## Loops
- Review may loop with Developer up to 3 times. On the 3rd rejection the ticket escalates to human.
