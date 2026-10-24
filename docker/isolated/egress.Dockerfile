# The allowlist proxy (`./run.sh --isolated`). tinyproxy from current Alpine
# (1.11.2: CVE-2023-49606 fixed), config baked in, run unprivileged.
FROM alpine:3.21
RUN apk add --no-cache tinyproxy && tinyproxy -v
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY egress-proxy.sh /app/egress-proxy.sh
USER 65534:65534
ENTRYPOINT ["/bin/sh", "/app/egress-proxy.sh"]
