---
name: incident-response
description: Procedure for a production incident — stabilize first, root-cause second
triggers: [incident, outage, production down, prod issue, sev, postmortem, rollback]
source: builtin
---

Stop the bleeding before you diagnose. Communicate throughout.

1. **Acknowledge + assess** severity and blast radius (who/what is affected). Start a timeline log.
2. **Mitigate fast**: roll back the last deploy, disable the feature flag, scale up, or fail over — restore service BEFORE finding the root cause.
3. **Communicate** status to stakeholders at a steady cadence.
4. **Diagnose** with logs/metrics/traces (see `observability-logging`): what changed? recent deploy/config/traffic? Form one hypothesis at a time.
5. **Fix forward** with a tested change once stable; verify recovery against the metrics that flagged it.
6. **Blameless postmortem**: timeline, root cause, what made it worse/better, concrete action items to prevent recurrence.
