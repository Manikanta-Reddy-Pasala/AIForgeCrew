# headroom (github.com/headroomlabs-ai/headroom) — local LLM-traffic
# compressor. AIForgeCrew runs it as a transparent FORWARDING proxy: the app
# repoints its single local model endpoint at this container (see
# llm/headroom.proxy_base), which compresses each request and forwards it to
# the real model (OPENAI_TARGET_API_URL). ONE mechanism covers every agent —
# ADK and client.py alike — because both resolve their base_url through that
# redirect. Enabled app-side by AIFORGE_HEADROOM=1; upstream + kompress flags
# come from the compose service env.
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
# Forward target comes from OPENAI_TARGET_API_URL (compose env, driven by
# AIFORGE_HEADROOM_UPSTREAM) — NOT a hardcoded --openai-api-url flag, which
# would override the env. The proxy compresses /v1/chat/completions and
# forwards to that upstream.
CMD ["headroom", "proxy", "--host", "0.0.0.0", "--port", "8787"]
