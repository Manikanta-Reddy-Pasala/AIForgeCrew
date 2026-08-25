"""Loader + validator for ``aiforge_core/agents.yaml``.

Reads the per-agent contracts (identity, tools.allowed/forbidden, memory
scopes, termination contracts) and exposes them as ``AgentContract``
dataclasses. ADK and the GA handler consume the same dataclass so the
three enforcement layers (structural filter, runtime reject, harness
pre-flight) cannot drift from a single YAML source.

Network-free: model paths are NOT verified against running servers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


_DEFAULT_PATH = Path(__file__).resolve().parent / "agents.yaml"

_KNOWN_BACKENDS = {
    "direct_litellm",
    "codeagent_smolagents",
    "genericagent_text_protocol",
    "adk_agent_with_ga",
    "adk_agent_direct_litellm",
}

_KNOWN_RUNTIMES = {
    "external_operator",
    "adk_agent_with_ga",
    "adk_agent_direct_litellm",
}

_VALID_READ_SCOPES = {"full", "scoped", "none"}
_FORBIDDEN_ALL_KEYWORD = "ALL"

_TURNS_MIN, _TURNS_MAX = 1, 100
_WALL_MIN_S, _WALL_MAX_S = 10, 7200


@dataclass(frozen=True)
class Identity:
    runtime: str
    model: str
    backend: str
    base_url: str
    ctx_window: int


@dataclass(frozen=True)
class Contract:
    inputs: list[str]
    outputs: list[str]
    max_turns: int
    max_wall_s: int


@dataclass(frozen=True)
class Tools:
    allowed: list[str]
    forbidden: list[str]
    forbidden_is_all: bool = False


@dataclass(frozen=True)
class Memory:
    read_scope: str
    write_scope: str


@dataclass(frozen=True)
class AgentContract:
    role: str
    identity: Identity
    contract: Contract
    tools: Tools
    memory: Memory
    rule: str
    termination_contract: list[str]
    editor_commands: list[str] | None = None


class AgentSpecError(ValueError):
    """Raised on malformed ``agents.yaml`` content."""


def _require(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise AgentSpecError(f"missing required field {key!r} in {where}")
    return d[key]


def _as_list_str(val: Any, where: str) -> list[str]:
    if val is None:
        return []
    if not isinstance(val, list):
        raise AgentSpecError(f"{where} must be a list, got {type(val).__name__}")
    out: list[str] = []
    for i, item in enumerate(val):
        if not isinstance(item, str):
            raise AgentSpecError(
                f"{where}[{i}] must be a string, got {type(item).__name__}"
            )
        out.append(item)
    return out


def _parse_one(role: str, raw: dict) -> AgentContract:
    where = f"agents.{role}"
    ident_raw = _require(raw, "identity", where)
    contract_raw = _require(raw, "contract", where)
    tools_raw = _require(raw, "tools", where)
    memory_raw = _require(raw, "memory", where)
    rule = _require(raw, "rule", where)
    term_raw = _require(raw, "termination_contract", where)

    identity = Identity(
        runtime=str(_require(ident_raw, "runtime", f"{where}.identity")),
        model=str(_require(ident_raw, "model", f"{where}.identity")),
        backend=str(_require(ident_raw, "backend", f"{where}.identity")),
        base_url=str(_require(ident_raw, "base_url", f"{where}.identity")),
        ctx_window=int(_require(ident_raw, "ctx_window", f"{where}.identity")),
    )

    contract = Contract(
        inputs=_as_list_str(_require(contract_raw, "inputs", f"{where}.contract"),
                            f"{where}.contract.inputs"),
        outputs=_as_list_str(_require(contract_raw, "outputs", f"{where}.contract"),
                             f"{where}.contract.outputs"),
        max_turns=int(_require(contract_raw, "max_turns", f"{where}.contract")),
        max_wall_s=int(_require(contract_raw, "max_wall_s", f"{where}.contract")),
    )

    allowed = _as_list_str(tools_raw.get("allowed"), f"{where}.tools.allowed")
    forbidden_raw = _as_list_str(tools_raw.get("forbidden"),
                                 f"{where}.tools.forbidden")
    forbidden_is_all = (
        len(forbidden_raw) == 1 and forbidden_raw[0] == _FORBIDDEN_ALL_KEYWORD
    )

    memory = Memory(
        read_scope=str(_require(memory_raw, "read_scope", f"{where}.memory")),
        write_scope=str(_require(memory_raw, "write_scope", f"{where}.memory")),
    )

    if not isinstance(rule, str) or not rule.strip():
        raise AgentSpecError(f"{where}.rule must be a non-empty string")

    termination = _as_list_str(term_raw, f"{where}.termination_contract")
    if not termination:
        raise AgentSpecError(f"{where}.termination_contract must be non-empty")

    editor_commands_raw = raw.get("editor_commands")
    if editor_commands_raw is not None:
        editor_commands = _as_list_str(
            editor_commands_raw, f"{where}.editor_commands",
        )
    else:
        editor_commands = None

    return AgentContract(
        role=role,
        identity=identity,
        contract=contract,
        tools=Tools(allowed=allowed, forbidden=forbidden_raw,
                    forbidden_is_all=forbidden_is_all),
        memory=memory,
        rule=rule.strip(),
        termination_contract=termination,
        editor_commands=editor_commands,
    )


def load_agents(path: Path | None = None) -> dict[str, AgentContract]:
    """Parse the agents YAML and return ``{role: AgentContract}``."""
    p = path if path is not None else _DEFAULT_PATH
    if not p.exists():
        raise AgentSpecError(f"agents yaml not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise AgentSpecError(f"agents yaml parse error: {exc}") from exc
    if not isinstance(raw, dict):
        raise AgentSpecError("top-level YAML must be a mapping")
    if "schema_version" not in raw:
        raise AgentSpecError("missing top-level field 'schema_version'")
    agents = raw.get("agents")
    if not isinstance(agents, dict) or not agents:
        raise AgentSpecError("missing or empty 'agents' mapping")
    out: dict[str, AgentContract] = {}
    for role, body in agents.items():
        if not isinstance(body, dict):
            raise AgentSpecError(f"agents.{role} must be a mapping")
        out[role] = _parse_one(role, body)
    return out


def _identity_violations(role: str, c) -> list[str]:
    """Runtime / backend / ctx_window checks for one contract's identity."""
    v: list[str] = []
    if c.identity.runtime not in _KNOWN_RUNTIMES:
        v.append(f"{role}: unknown runtime {c.identity.runtime!r} "
                 f"(expected one of {sorted(_KNOWN_RUNTIMES)})")
    if c.identity.backend not in _KNOWN_BACKENDS:
        v.append(f"{role}: unknown backend {c.identity.backend!r} "
                 f"(expected one of {sorted(_KNOWN_BACKENDS)})")
    if c.identity.ctx_window <= 0:
        v.append(f"{role}: ctx_window must be positive, got {c.identity.ctx_window}")
    return v


def _limit_violations(role: str, c) -> list[str]:
    """max_turns / max_wall_s range checks."""
    v: list[str] = []
    if not (_TURNS_MIN <= c.contract.max_turns <= _TURNS_MAX):
        v.append(f"{role}: max_turns {c.contract.max_turns} outside "
                 f"[{_TURNS_MIN}, {_TURNS_MAX}]")
    if not (_WALL_MIN_S <= c.contract.max_wall_s <= _WALL_MAX_S):
        v.append(f"{role}: max_wall_s {c.contract.max_wall_s} outside "
                 f"[{_WALL_MIN_S}, {_WALL_MAX_S}]")
    return v


def _tools_violations(role: str, c) -> list[str]:
    """allowed / forbidden consistency checks."""
    v: list[str] = []
    allowed_set = set(c.tools.allowed)
    forbidden_set = set() if c.tools.forbidden_is_all else set(c.tools.forbidden)
    overlap = allowed_set & forbidden_set
    if overlap:
        v.append(f"{role}: tools appear in both allowed and forbidden: "
                 f"{sorted(overlap)}")
    if c.tools.forbidden_is_all and allowed_set:
        v.append(f"{role}: forbidden=ALL but allowed list is non-empty "
                 f"({sorted(allowed_set)})")
    return v


def _memory_io_violations(role: str, c) -> list[str]:
    """memory scope + contract inputs/outputs presence checks."""
    v: list[str] = []
    if c.memory.read_scope not in _VALID_READ_SCOPES:
        v.append(f"{role}: memory.read_scope {c.memory.read_scope!r} "
                 f"not in {sorted(_VALID_READ_SCOPES)}")
    ws = c.memory.write_scope
    if not isinstance(ws, str) or not ws.strip():
        v.append(f"{role}: memory.write_scope must be non-empty")
    if not c.contract.inputs:
        v.append(f"{role}: contract.inputs must be non-empty")
    if not c.contract.outputs:
        v.append(f"{role}: contract.outputs must be non-empty")
    return v


def validate_contracts(contracts: dict[str, AgentContract]) -> list[str]:
    """Return a list of human-readable violations (empty list = OK).

    Performs static checks only — does not touch the network or any
    running model server.
    """
    violations: list[str] = []
    for role, c in contracts.items():
        violations += _identity_violations(role, c)
        violations += _limit_violations(role, c)
        violations += _tools_violations(role, c)
        violations += _memory_io_violations(role, c)
    return violations


def _entry_tool_name(entry: dict) -> "str | None":
    """The tool name of one schema entry — a top-level ``name`` or, for a
    ``{"type": "function", "function": {...}}`` wrapper, the function's name."""
    name = entry.get("name")
    if isinstance(name, str):
        return name
    if entry.get("type") == "function":
        fn = entry.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            return fn["name"]
    return None


def tools_schema_for_role(
    role: str,
    full_schema: list[dict],
    contracts: dict[str, AgentContract] | None = None,
) -> list[dict]:
    """Filter a full GA tool-schema list down to the role's allowed tools.

    ``full_schema`` is a list of dicts each carrying a ``"name"`` key
    (the standard JSON-Schema shape used by GA / ADK). The returned list
    preserves input order. Tools whose name is not in the role's allowed
    list are dropped — that is the structural Layer A filter.

    If the role's forbidden list is the keyword ``ALL``, the returned
    list is empty regardless of allowed (defense-in-depth: matches the
    runtime semantics in the GA handler).
    """
    if contracts is None:
        contracts = load_agents()
    if role not in contracts:
        raise AgentSpecError(f"unknown role: {role}")
    c = contracts[role]
    if c.tools.forbidden_is_all:
        return []
    allowed = set(c.tools.allowed)
    forbidden = set(c.tools.forbidden)
    filtered: list[dict] = []
    for entry in full_schema:
        if not isinstance(entry, dict):
            continue
        name = _entry_tool_name(entry)
        if name is None or name in forbidden:
            continue
        if allowed and name not in allowed:
            continue
        filtered.append(entry)
    return filtered


__all__ = [
    "AgentContract",
    "AgentSpecError",
    "Contract",
    "Identity",
    "Memory",
    "Tools",
    "load_agents",
    "tools_schema_for_role",
    "validate_contracts",
]
