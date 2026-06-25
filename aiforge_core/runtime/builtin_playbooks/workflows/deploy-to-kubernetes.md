---
name: deploy-to-kubernetes
description: Procedure to deploy/update a service on Kubernetes safely
triggers: [kubernetes, k8s, deploy, rollout, kubectl, helm, manifest]
source: builtin
---

1. **Build + push** a versioned image (immutable tag, not `latest`); record the digest.
2. **Update manifests/Helm values** with the new tag; set resource requests/limits, liveness/readiness probes, replicas.
3. **Validate** before applying: `kubectl diff` / `helm diff`, lint the manifests. Dry-run on a non-prod namespace first.
4. **Roll out** (`kubectl apply` / `helm upgrade`) — Kubernetes does a rolling update; readiness probe gates traffic.
5. **Watch it**: `kubectl rollout status`, pod logs, error/latency dashboards. New pods Ready and healthy?
6. **Rollback fast if bad**: `kubectl rollout undo` / `helm rollback`. Don't debug a broken deploy in prod — revert, then investigate.
7. **Secrets/config** via Secret/ConfigMap, never baked into the image.
