# The guarded default's firewall keeper (`./run.sh`): iptables for the box's
# cgroup only. Needs the host network and NET_ADMIN — nothing else.
FROM alpine:3.21
RUN apk add --no-cache iptables ip6tables curl
COPY egress-guard.sh /app/egress-guard.sh
ENTRYPOINT ["/bin/sh", "/app/egress-guard.sh"]
