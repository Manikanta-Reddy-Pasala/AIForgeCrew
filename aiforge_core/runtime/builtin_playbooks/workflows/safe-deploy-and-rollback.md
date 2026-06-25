---
name: safe-deploy-and-rollback
description: Procedure for a low-risk deploy with a fast rollback path
triggers: [deploy, release, rollout, blue green, canary, rollback, ship to prod]
source: builtin
---

1. **Pre-flight**: CI green, migrations are backward-compatible (expand→contract), a fresh DB backup, feature behind a flag if risky.
2. **Choose the strategy**: rolling, blue-green (switch traffic between two identical envs), or canary (small % first). Keep the previous version live/ready.
3. **Deploy the new version** to the standby/canary; run smoke tests against it BEFORE shifting real traffic.
4. **Shift traffic gradually**; watch error rate + latency + business metrics at each step.
5. **Bake**: hold and observe before declaring success.
6. **Rollback plan is the deploy plan**: if metrics regress, switch traffic back / `rollout undo` immediately — revert first, diagnose after.
7. **Post-deploy**: confirm health, then retire the old version. Decouple deploy (ship code, dark) from release (flip the flag) for the riskiest changes.
