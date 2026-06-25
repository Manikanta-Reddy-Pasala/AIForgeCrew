---
name: run-load-test
description: Procedure to load-test a service and find its limits
triggers: [load test, stress test, benchmark, throughput, capacity, k6, jmeter, locust]
source: builtin
---

1. **Define the goal + SLO**: target RPS, p95/p99 latency, error rate, the scenario (realistic mix, not one endpoint).
2. **Use a representative env** (prod-like, isolated) with realistic data volume — testing against an empty DB lies.
3. **Script the scenario** (k6/Locust/JMeter): ramp up gradually; include think-time + a realistic request mix.
4. **Measure**: latency percentiles (not just average), throughput, error rate, AND server-side resource saturation (CPU/mem/connections/DB).
5. **Find the knee**: increase load until latency/errors break the SLO — that's your capacity. Identify the bottleneck (app/db/network).
6. **Fix the bottleneck** (see `performance-optimization` skill) and re-test against the same baseline.
7. Document numbers + the limiting resource; set capacity/alerts from real data.
