# The box's way to the model endpoint (`./run.sh --isolated`): one fixed upstream.
FROM alpine:3.21
RUN apk add --no-cache socat ca-certificates
COPY model-relay.sh /app/model-relay.sh
USER 65534:65534
ENTRYPOINT ["/bin/sh", "/app/model-relay.sh"]
