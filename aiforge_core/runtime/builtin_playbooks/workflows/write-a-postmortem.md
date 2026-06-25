---
name: write-a-postmortem
description: Procedure to write a blameless postmortem after an incident
triggers: [postmortem, retrospective, retro, incident review, root cause analysis, rca]
source: builtin
---

After the incident is resolved (see `incident-response`), write the doc that prevents the next one.

1. **Summary**: what happened, impact (who/what/how long), severity — one paragraph.
2. **Timeline**: detection → diagnosis → mitigation → resolution, with timestamps. Facts, not blame.
3. **Root cause**: the technical cause AND the contributing factors (why it wasn't caught earlier, why it spread). Use "5 whys".
4. **What helped / what hurt**: what made detection/mitigation faster or slower.
5. **Action items**: concrete, owned, dated changes to prevent recurrence or reduce blast radius/MTTR — not "be more careful."
6. **Blameless**: focus on systems and gaps, not people. The goal is learning, so people report honestly.
7. Share it; track the action items to done.
