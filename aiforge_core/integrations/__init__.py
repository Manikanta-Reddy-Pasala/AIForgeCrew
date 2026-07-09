"""Third-party library adapters — the ONLY place external integration libs
are imported.

Separation of concerns: domain code (llm/, runtime/, scripts/) never imports
``instructor`` / ``crawl4ai`` / ``ragas`` directly — it calls the thin adapter
here. Every adapter follows the same contract:

- ``available() -> bool`` — cheap import probe; False when the optional extra
  isn't installed (deps are optional: ``pip install aiforgecrew[structured]``,
  ``[crawl]``, ``[evals]`` — see pyproject).
- One narrow function per capability, typed on OUR domain shapes (dicts /
  pydantic models), never on library types — so a library swap or removal
  touches one file.
- Adapters RAISE on failure; the DOMAIN caller owns fallback policy (e.g.
  llm/structured.py falls back to its own reask loop, web_ingest falls back
  to plain HTTP fetch).

The chonkie chunking adapter lives in the standalone ``aiforge_memory``
package (``features/chunk/chonkie_adapter.py``) — same contract, kept there
because that package must not depend on aiforge_core.
"""
