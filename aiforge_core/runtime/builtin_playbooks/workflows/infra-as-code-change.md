---
name: infra-as-code-change
description: Procedure to change cloud infra via Terraform/IaC safely
triggers: [terraform, infrastructure, iac, provision, cloud, plan apply, cloudformation]
source: builtin
---

1. **Branch + edit the IaC** (Terraform/Pulumi/CloudFormation) — never click-change prod infra by hand; it drifts from code.
2. **`plan`/preview** and READ it carefully: what's created, changed, and especially DESTROYED/replaced. A replace on a stateful resource (DB, disk) can mean data loss.
3. **Review** the plan like a code review; for destructive/stateful changes get a second pair of eyes.
4. **Apply to non-prod first**; verify the resources + the app on them.
5. **Apply to prod** in a controlled window; keep the state file locked/remote so two people can't apply at once.
6. **Verify** the live infra matches intent; check no drift, no orphaned/leaked resources.
7. **Secrets/state** stay out of the repo; state backend is encrypted + access-controlled.
