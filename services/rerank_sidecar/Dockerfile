# bge-reranker-v2-m3 cross-encoder sidecar — serves /rerank on :8765.
FROM python:3.12-slim

# One layer: the unprivileged user and the CA bundle are both "prepare the
# box", and splitting them cost an image layer for nothing.
# Serving a model needs no privileges; the user is created BEFORE pip and
# the model cache so those layers are owned by whoever will read them.
RUN useradd --create-home --uid 10001 sidecar \
    && apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./

EXPOSE 8765
USER sidecar
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8765"]
