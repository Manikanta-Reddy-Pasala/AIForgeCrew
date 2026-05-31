# tickets.oneshell.in public exposure (rebuilt 2026-05-30)

Old path (nginx+LE on a standalone VPS at 77.42.68.16) was deleted with that VM.
Rebuilt to ride the existing prod Kubernetes ingress instead of a dedicated VPS.

## Live topology

```
browser
  └─ https://tickets.oneshell.in            (Cloudflare proxied A → 5.161.38.54)
       └─ Traefik LB 5.161.38.54:443         (wildcard *.oneshell.in cert: secret oneshell-credential)
            └─ k8s Service/Endpoints tickets-nuc → 178.156.146.205:8799   (Harbor VM, public)
                 └─ socat tickets-bridge.service  :8799 → 10.13.13.2:8799  (over WireGuard wg0)
                      └─ NUC ai@192.168.70.115 aiforge-api :8799
```

WireGuard: VM 178.156.146.205 (wg0, 10.13.13.1, :51820) ↔ NUC (wg1, 10.13.13.2).
The Harbor VM already owned 80/443 (goharbor), so a dedicated TLS frontend there was
impossible — hence routing through prod Traefik (which already terminates *.oneshell.in)
and using the VM only as a WireGuard bridge.

## Re-apply
- k8s:   KUBECONFIG=~/.kube/prod-config kubectl --insecure-skip-tls-verify apply -f tickets-ingress.yaml
- VM WG:        /etc/wireguard/wg0.conf  (server pubkey bO+9Ch8A4Y6Z12a9loABciEsGi8Z32uuJw/j9kQNLCo=)
- VM bridge:    /etc/systemd/system/tickets-bridge.service  (socat :8799 → 10.13.13.2:8799)
- NUC peer:     /etc/wireguard/wg1.conf  (Endpoint 178.156.146.205:51820, peer = VM pubkey above)
- CF DNS:       zone f4b97e0d… record 05431401583e88f8a8fd427fa485f721 → A 5.161.38.54 proxied

## Hardening TODO
- VM :8799 is currently world-open (ufw inactive on the Harbor box; enabling ufw there
  risks SSH lockout, deferred). Same data is public via tickets.oneshell.in anyway, but
  the direct VM:8799 path bypasses the Cloudflare WAF. Lock to Traefik egress when convenient.
