---
name: testing-live-systems
description: Probe and validate a REAL running system (API, service, DB, deploy) with the shell/http tools, not just unit tests
triggers: [test the live, hit the endpoint, is it up, smoke test prod, check the deploy, health check, curl the api, real system, staging, e2e against, verify the service]
source: builtin
---

You have `run_command`, `http_get`/`http_request`, and file tools — use them to test the system that is ACTUALLY running, not a mock. Never claim "it works" from code inspection alone; get evidence from the live system.

1. **Find the target.** Base URL / host+port / connection string. Confirm reachability first: `curl -s -o /dev/null -w '%{http_code}' <url>/health` (or `/`, `/actuator/health`, `/api/health`). Connection-refused ≠ a bug in the code — the service is down; say so.
2. **Assert on responses, not vibes.** For each endpoint: status code, key response fields (parse the JSON — `| python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["x"]==...'`), and latency if it matters. Quote the actual output.
3. **Walk the real flow.** Happy path end-to-end as a client would (create → read → update → delete), then the error paths (400 on bad input, 401/403 unauthorized, 404 missing, 429 rate-limit). Verify side effects at the source — a DB row, an emitted event, a written file, a log line — not just the HTTP 200.
4. **Use the least-privilege path + non-prod first.** Prefer staging/QA. Against prod: read-only checks only unless explicitly authorized; never mutate prod data as a "test". No secrets in commands or logs.
5. **Make it repeatable.** Put the checks in a script (`smoke.sh` / a pytest hitting the live URL via `requests`) so the same evidence can be re-run, not a one-off you can't reproduce.
6. **Report the evidence.** Command → observed status/body → pass/fail per check. If a check fails, capture the failing response + the relevant server log before proposing a fix.

Anti-patterns: asserting success without reading the body; testing only the happy path; hammering prod; leaving no reproducible script; treating a 500 as "flaky" instead of reading the logs.
