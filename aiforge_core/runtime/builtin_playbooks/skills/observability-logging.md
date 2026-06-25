---
name: observability-logging
description: Add logs, metrics, and traces that make incidents debuggable
triggers: [logging, logs, observability, metrics, tracing, monitoring, debug]
source: builtin
---

You can't fix what you can't see. Instrument for the 3am incident.

- **Structured logs** (key=value / JSON), not string soup. Include a correlation/request id to stitch a flow.
- **Right level**: ERROR = needs action; WARN = recoverable oddity; INFO = key business events; DEBUG = dev detail (off in prod).
- **Log the meaningful thing**, not a stack-trace wall for an expected error. Never log secrets/PII/tokens.
- **Metrics** for rates/latency/errors (RED) and resource saturation (USE). Alert on symptoms users feel, not noise.
- **Traces** across service boundaries so a slow request shows WHERE the time went.
- Make failures actionable: the error message should say what failed and what to do.
