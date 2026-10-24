# The box's SSH exit (`./run.sh --isolated`): CONNECT to SSH ports only.
FROM python:3.12-alpine
COPY ssh-egress.py /app/ssh-egress.py
USER 65534:65534
ENTRYPOINT ["python3", "-u", "/app/ssh-egress.py"]
