"""Per-role tool scoping for ``agent_config`` (split submodule).

Parsed from ``aiforge_core/agents/agents.yaml`` (the SAME source the ADK /
GA / harness layers read). Lets the tool factory hand each agent only the
tools its role is permitted — a security/scoping backstop that no longer
relies on the model honouring a prompt contract. Soft-fail: any parse error
→ allow-all (never break the pipeline build over a malformed YAML).
"""
from __future__ import annotations

import logging
from typing import Any

_AGENTS_CONTRACTS_CACHE: dict[str, Any] = {"loaded": False, "contracts": None}


def _agent_contracts() -> "dict[str, Any] | None":
    """Lazy-load + cache the ``agents.yaml`` contracts. ``None`` on failure.

    Cached once — the YAML header states changes take effect on graph-runner
    restart only, so re-reading per call would be wasted IO.
    """
    if _AGENTS_CONTRACTS_CACHE["loaded"]:
        return _AGENTS_CONTRACTS_CACHE["contracts"]
    contracts: "dict[str, Any] | None"
    try:
        from aiforge_core.agents import loader as _loader
        contracts = _loader.load_agents()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("aiforge.agent_config").warning(
            "agents.yaml unreadable for tool enforcement (%s) — enforcement "
            "disabled (all tools allowed).", exc)
        contracts = None
    _AGENTS_CONTRACTS_CACHE["contracts"] = contracts
    _AGENTS_CONTRACTS_CACHE["loaded"] = True
    return contracts


def allowed_tools_for(role: str) -> "tuple[frozenset[str] | None, frozenset[str]]":
    """Return ``(allowed_or_None, forbidden)`` tool-name sets for ``role``.

    Parsed from ``agents.yaml``. Semantics (matches the loader/GA layer):

      * ``allowed`` absent / empty / ``["all"]`` / ``["*"]`` → ``allowed=None``
        (no allowlist restriction; every tool passes except ``forbidden``).
      * ``allowed`` = explicit list → only those tool names pass.
      * ``forbidden = ["ALL"]`` → ``allowed=frozenset()`` (an EXPLICIT empty
        allowlist: nothing passes — a hard tool-less role).
      * ``forbidden`` list → always removed, even if also in ``allowed``.
      * unknown role / missing / malformed yaml → ``(None, frozenset())``
        i.e. allow-all — the backward-compatible default so a missing config
        never suddenly restricts an existing run.

    Names are matched verbatim against the tool's function name
    (``FunctionTool.name`` == ``func.__name__``) by the caller.
    """
    contracts = _agent_contracts()
    if not contracts or role not in contracts:
        return None, frozenset()
    tools = getattr(contracts[role], "tools", None)
    if tools is None:
        return None, frozenset()
    if getattr(tools, "forbidden_is_all", False):
        # forbidden=ALL → explicit empty allowlist (zero tools).
        return frozenset(), frozenset()
    allowed_list = list(getattr(tools, "allowed", None) or [])
    forbidden = frozenset(getattr(tools, "forbidden", None) or [])
    lowered = {a.strip().lower() for a in allowed_list}
    if not allowed_list or lowered <= {"all", "*"}:
        return None, forbidden
    return frozenset(allowed_list), forbidden
