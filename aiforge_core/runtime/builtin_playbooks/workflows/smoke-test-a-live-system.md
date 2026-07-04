---
name: smoke-test-a-live-system
description: Procedure to smoke-test a real deployed service end-to-end and produce a pass/fail report with evidence
triggers: [smoke test the deploy, test the running service, validate staging, verify the deployment, e2e live, is the service healthy, post-deploy check]
source: builtin
---

Goal: prove a live service actually works, with reproducible evidence. Prefer staging/QA; read-only against prod unless authorized.

1. **Scope it.** List the endpoints/flows that matter for "working" (health, auth, the 2-3 core user actions). Get the base URL + any test credentials (never hard-code secrets — read from env).
2. **Reachability + health.** `curl -sS -o /dev/null -w '%{http_code} %{time_total}s\n' <base>/health`. If unreachable, stop and report the service is down (not a code bug).
3. **Auth.** Obtain a token/session the real way; confirm a protected endpoint returns 401 without it and 200 with it.
4. **Core flows.** For each: issue the request, assert status + parsed response fields, then verify the side effect at the source (DB/queue/file/log). Do create→read→update→delete so you leave no test litter (or clean up what you create).
5. **Error paths.** Bad input → 400, missing → 404, unauthorized → 403, over-limit → 429. Confirm the service degrades correctly, not with a 500.
6. **Bundle it.** Write the checks into `smoke.sh` (or `test_smoke.py` using `requests`) so anyone can re-run `./smoke.sh <base-url>` and get the same verdict.
7. **Report.** A table: check → command → observed → PASS/FAIL. On any FAIL, attach the failing response body + the matching server log excerpt. Do not report "all good" if any check was skipped — say which and why.

Guardrails: no prod mutations without explicit sign-off; no secrets in commands/output/scripts; back off on 429 rather than hammering.
