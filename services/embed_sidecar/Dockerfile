# bge-m3 ONNX embedding sidecar — serves /embed + /embed_batch on :8764
# (the API's aiforge_core.memory.embed client talks this shape).
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

# Model must be PRE-STAGED into BGE_M3_DIR on the mounted /models volume.
# The sidecar runs offline (local_files_only) and never downloads at runtime —
# if the model is absent it stays up and returns 503 (graceful degrade).
ENV BGE_M3_DIR=/models/bge-m3
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
EXPOSE 8764
USER sidecar
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8764"]
