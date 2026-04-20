# Developer — Contract

## Inputs
- One child ticket body + orchestrator context bundle
- On retry: review comments from Architect

## Outputs
- Patches applied via write_file
- Commits on `feat/<child-id>` branch
- `run_tests` results with pass/fail counts
- `report(status, summary, confidence, ...)`

## Loops
- Retry same child after review reject, max 3. Then escalate.
