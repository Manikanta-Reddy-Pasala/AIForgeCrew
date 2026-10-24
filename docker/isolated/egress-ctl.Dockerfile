# The allowlist's owner (`./run.sh --isolated`). /policy is created here owned
# by the unprivileged user, so a fresh named volume starts writable by it.
FROM python:3.12-alpine
RUN mkdir -p /policy && chown 65534:65534 /policy
COPY egress-controller.py /app/egress-controller.py
USER 65534:65534
ENTRYPOINT ["python3", "-u", "/app/egress-controller.py"]
