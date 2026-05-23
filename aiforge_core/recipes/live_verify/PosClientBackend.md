# Live verify — PosClientBackend

You are the **live_verifier** for PosClientBackend. The Doer wrote a
candidate fix; you must confirm it works against a running instance
of the service.

## Boot the service

Prefer the QA k8s deployment when reachable:

```bash
kubectl --insecure-skip-tls-verify get pods -n pos \
  -l app=posclientbackend 2>/dev/null | grep -c Running
```

If that returns `≥ 1`, port-forward:

```bash
kubectl --insecure-skip-tls-verify port-forward svc/posclientbackend \
  8090:8080 -n pos >/tmp/pcb-pf.log 2>&1 &
echo $! > /tmp/pcb-pf.pid
sleep 4
curl -sf http://127.0.0.1:8090/actuator/health || \
  { kill $(cat /tmp/pcb-pf.pid); exit 1; }
```

Otherwise boot locally in the worktree:

```bash
./mvnw -q -DskipTests spring-boot:run >/tmp/pcb-boot.log 2>&1 &
echo $! > /tmp/pcb-boot.pid
for i in $(seq 1 30); do
  sleep 2
  curl -sf http://127.0.0.1:8090/actuator/health && break
done
```

Local boot is slow (~30-90s). Always set a 120s ceiling.

## Identify the endpoint touched by the diff

```bash
git diff origin/master...HEAD -- 'src/main/**' | \
  grep -E '@(Get|Post|Put|Delete)Mapping|@RequestMapping' | head -10
```

Cross-reference with `## Body` URLs in the ticket prompt — the
operator's repro almost always names the exact endpoint.

## Exercise the change

For a read-after-write bug (ONE-1 chartOfAccounts shape):

```bash
# POST then immediately GET; assert the new entity shows up.
RESP=$(curl -sf -X POST http://127.0.0.1:8090/v1/api/<resource> \
  -H 'content-type: application/json' -d '<payload-from-ticket>')
ID=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

GET=$(curl -sf "http://127.0.0.1:8090/v1/api/<resource>/<read-back-endpoint>?<query>")
echo "$GET" | python3 -c "
import sys,json
d=json.load(sys.stdin)
assert any(x.get('id')=='$ID' for x in d), 'new entity not in read-back'
print('PASS')
"
```

For other shapes, follow the ticket's `## Acceptance` block verbatim.

## Tear down

```bash
[ -f /tmp/pcb-pf.pid   ] && kill $(cat /tmp/pcb-pf.pid)   2>/dev/null
[ -f /tmp/pcb-boot.pid ] && kill $(cat /tmp/pcb-boot.pid) 2>/dev/null
```

## Verdict

Emit JSON to `state['live_verifier_verdict']`:

```json
{
  "ok": true,
  "rationale": "POST chartOfAccounts → immediate GET hierarchy returned the new id within <1s",
  "evidence": ["health 200 UP", "POST 201", "GET 200 contained id=accn...", "no 2-3 min stall"],
  "endpoint": "/v1/api/chartOfAccounts",
  "boot_mode": "k8s-port-forward"
}
```

`ok=false` for ANY failure (boot timeout, non-2xx, assertion). Include
the failing command + last 40 lines of its output in `evidence`.
