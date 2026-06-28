# multi_repo — codemem L2 fixture

Two-service mini-repo:

- `api/` — FastAPI HTTP service on port 8080
- `worker/` — NATS consumer for `business.push.request`

## Build / run

    pip install -r requirements.txt
    python -m api.main          # api on :8080
    python -m worker.main       # worker subscribes to NATS
    pytest tests/

## Port-forward (k8s)

    kubectl port-forward svc/api 8080:8080 -n default
