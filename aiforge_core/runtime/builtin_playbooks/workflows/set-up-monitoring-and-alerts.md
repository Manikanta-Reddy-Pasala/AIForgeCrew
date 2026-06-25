---
name: set-up-monitoring-and-alerts
description: Procedure to make a service observable and alert on real problems
triggers: [monitoring, alerting, dashboard, metrics, alerts, sli, slo, on call]
source: builtin
---

1. **Define SLIs/SLOs**: the few signals users feel — availability, latency (p95/p99), error rate, and key business events.
2. **Instrument** the service (see `observability-logging` skill): structured logs with correlation ids, RED/USE metrics, traces across boundaries.
3. **Dashboards** for those signals — at-a-glance health, plus drill-down for diagnosis.
4. **Alert on symptoms, not causes**: page when users are affected (SLO burn, error spike), not on every blip. Tune thresholds to avoid noise → alert fatigue.
5. **Every alert is actionable**: a clear title, what it means, and a runbook link. If you can't act on it, it's a dashboard, not an alert.
6. **Test the alert path** (fire a synthetic) and the on-call escalation.
7. Review noisy/missed alerts after incidents; adjust.
