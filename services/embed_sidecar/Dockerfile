# bge-m3 ONNX embedding sidecar — serves /embed + /embed_batch on :8764
# (the API's aiforge_core.memory.embed client talks this shape).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./

# Model is fetched into BGE_M3_DIR on first boot (huggingface-hub) and
# cached on the mounted /models volume.
ENV BGE_M3_DIR=/models/bge-m3
EXPOSE 8764
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8764"]
