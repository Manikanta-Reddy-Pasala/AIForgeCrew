"""LSP-driven symbol resolution.

Architecture:
    client.LspClient        — minimal stdio JSON-RPC client.
    adapters.adapter_for    — picks a server command per language.
    resolver.resolve_calls  — high-confidence CALLS via textDocument/references.

Opt-in: enable via `aiforge-memory ingest --lsp`. When the chosen
language server is not installed, the resolver returns an empty list and
the existing tree-sitter heuristic remains the source of truth.
"""

from aiforge_memory.features.lsp.adapters import adapter_for, available_servers
from aiforge_memory.features.lsp.client import LspClient, LspError
from aiforge_memory.features.lsp.resolver import resolve_calls

__all__ = [
    "LspClient",
    "LspError",
    "adapter_for",
    "available_servers",
    "resolve_calls",
]
