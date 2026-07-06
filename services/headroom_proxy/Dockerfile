# headroom (github.com/headroomlabs-ai/headroom) — local LLM-traffic
# compressor. AIForgeCrew uses it in "library mode via the sidecar": the
# app's LLM client (llm/client.py) POSTs message lists to this container's
# /v1/compress endpoint (compress-ONLY, no LLM call), then sends the
# compressed messages on to each model's OWN endpoint itself. So this
# proxy never forwards inference and needs no fixed upstream — it only
# compresses. Enabled app-side by AIFORGE_HEADROOM=1.
FROM python:3.12-slim

# build-essential is needed at install time only: headroom-ai bundles
# hnswlib, a C++ extension compiled from source. Removed right after so
# the image stays slim (same pattern headroom's own docs recommend).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates \
    && pip install --no-cache-dir "headroom-ai[proxy]" \
    && apt-get purge -y build-essential && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 8787
# Only /v1/compress is used (compress-only, no forwarding), so the upstream
# --openai-api-url is irrelevant — left at the loopback model endpoint as a
# harmless default for anyone who also wants the passthrough routes.
CMD ["headroom", "proxy", "--host", "0.0.0.0", "--port", "8787", \
     "--openai-api-url", "http://127.0.0.1:1234"]
