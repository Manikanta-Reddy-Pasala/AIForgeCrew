# headroom (github.com/headroomlabs-ai/headroom) — local LLM-traffic
# compression proxy. Isolated test service: NOT wired into AIForgeCrew's
# own LLM calls yet (AIFORGE_LM_BASE_URL is untouched). Sits at :8787,
# forwards OpenAI-compatible traffic to the real LM Studio endpoint at
# 127.0.0.1:1234, so it can be curl-tested directly before anything in
# the app is repointed at it.
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
CMD ["headroom", "proxy", "--host", "0.0.0.0", "--port", "8787", \
     "--openai-api-url", "http://127.0.0.1:1234"]
