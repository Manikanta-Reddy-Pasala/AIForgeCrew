"""Orchestrator package.

Today only ships :mod:`llm_client` — a thin shim over ``aiforge_core.llm``
preserved for the API chat endpoint and any external consumers still
importing from this path. The full v3/v4 archetype runner was retired
in favour of the ADK pipeline (see ``aiforge_core.runtime.adk_runner``).
"""
from __future__ import annotations
