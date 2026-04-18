"""Hermes — per-agent runtime for AIForgeCrew.

Phase P2. Reads agent contract + permissions, builds system prompt,
dispatches tool calls through a permission-gated registry, pushes audit
events into Paperclip's store, and drives the chat-completion loop.
"""
from __future__ import annotations

__version__ = "0.1.0"
