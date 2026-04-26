"""Web search — Gemini 2.5-flash with googleSearch grounding.

Pure logic + schema. Handler in ``ga_runner.py`` thin-wraps
``handle()`` so the LM call is testable + isolated from GA.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Run a Google-grounded web search via Gemini 2.5-flash. "
            "Returns Gemini's curated answer + top citation URLs. "
            "Use for unknown-API recovery: when a 'cannot find symbol' "
            "compile error points at a class you don't recognize, "
            "search the official docs and patch with the right API."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Concise search query. Best results: include "
                        "framework + version + specific API. "
                        "e.g. 'Spring Data MongoDB Aggregation.group "
                        "with sum and count Java example'"
                    ),
                },
            },
            "required": ["query"],
        },
    },
}


_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-2.5-flash:generateContent"
)


def handle(args: dict) -> str:
    """Issue the grounded search; format result for the doer.

    Returns either a `[web_search] ...` error blob or
    ``<answer>\\n\\nCitations:\\n- ...`` ready to drop into a
    StepOutcome. Network-fail and missing-key paths produce
    explicit errors so the model can pivot to ask_explorer.
    """
    query = (args.get("query") or "").strip()
    if not query:
        return "[web_search] empty query"
    api_key = os.environ.get("AIFORGE_GOOGLE_API_KEY", "")
    if not api_key:
        return "[web_search] no AIFORGE_GOOGLE_API_KEY set"
    payload = json.dumps({
        "contents": [{"parts": [{"text": query}]}],
        "tools": [{"googleSearch": {}}],
    }).encode()
    req = urllib.request.Request(
        _GEMINI_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "X-goog-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        return f"[web_search] network error: {exc}"
    try:
        data = json.loads(body)
    except Exception as exc:
        return f"[web_search] bad response: {exc}"
    cand = (data.get("candidates") or [{}])[0]
    parts = cand.get("content", {}).get("parts", [])
    answer = "\n".join(p.get("text", "") for p in parts).strip()
    grounding = cand.get("groundingMetadata", {})
    chunks = grounding.get("groundingChunks", [])[:5]
    cites: list[str] = []
    for ch in chunks:
        web = ch.get("web") or {}
        title = (web.get("title") or "")[:120]
        uri = web.get("uri") or ""
        if uri:
            cites.append(f"- {title} | {uri}")
    out = answer[:4000]
    if cites:
        out += "\n\nCitations:\n" + "\n".join(cites)
    return out or "[web_search] empty answer"
