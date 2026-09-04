# bge-reranker-v2-m3 cross-encoder sidecar — serves /rerank on :8765.
FROM python:3.12-slim

# Serving a model needs no privileges. Created here so the layers below
# (pip, the model cache) are owned by the user that will read them.
RUN useradd --create-home --uid 10001 sidecar

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./

EXPOSE 8765
USER sidecar
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8765"]
