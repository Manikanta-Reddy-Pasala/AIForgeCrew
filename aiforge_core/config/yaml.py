"""Optional YAML config loader for AIForgeCrew.

Reads ``$AIFORGE_CONFIG`` (default ``~/.aiforge/aiforge.yaml``) at module
import time and exports any matching keys as environment variables —
*before* ``runtime.config`` reads them. Anything already in the
environment wins (env > yaml > defaults).

Schema (all fields optional):

    dsn:      postgresql://...           # AIFORGE_DSN

    inference:
      lm_url:    http://127.0.0.1:1234/v1
      lm_key:    lm-studio
      claude_bin: claude
      claude_model: claude-opus-4-7
      embed_url:  http://127.0.0.1:8764
      rerank_url: http://127.0.0.1:8765

    paths:
      log_dir:        ~/.aiforge/logs
      lock_dir:       /tmp
      worktree_root:  ~/codeRepo

    tick:
      max_wall_secs: 2400
      max_turns_ceiling: 300

    api:
      base: http://localhost:8799

    intent:
      enrich: true
      lm_url: http://127.0.0.1:1235/v1     # planner port

    codemem:
      query_enabled: true                   # AIFORGE_CODEMEM_QUERY=1

    neo4j:
      uri: bolt://127.0.0.1:7687
      user: neo4j
      password: password

Loaded automatically on first import of any aiforge_core.runtime module
when ``AIFORGE_CONFIG_AUTOLOAD`` is unset or != "0".
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore


_DEFAULT_PATH = os.environ.get(
    "AIFORGE_CONFIG", os.path.expanduser("~/.aiforge/aiforge.yaml")
)

# Mapping: yaml_path -> env_var (env wins if already set)
_FIELD_MAP: dict[tuple[str, ...], str] = {
    ("dsn",): "AIFORGE_DSN",
    ("inference", "lm_url"):       "AIFORGE_LM_BASE_URL",
    ("inference", "lm_key"):       "AIFORGE_LM_API_KEY",
    ("inference", "claude_bin"):   "AIFORGE_CLAUDE_BIN",
    ("inference", "claude_model"): "AIFORGE_CLAUDE_MODEL",
    ("inference", "embed_url"):    "AIFORGE_EMBED_URL",
    ("inference", "rerank_url"):   "AIFORGE_RERANK_URL",
    ("paths", "log_dir"):          "AIFORGE_LOG_DIR",
    ("paths", "lock_dir"):         "AIFORGE_LOCK_DIR",
    ("paths", "worktree_root"):    "AIFORGE_WORKTREE_ROOT",
    ("tick", "max_wall_secs"):     "AIFORGE_TICK_MAX_WALL",
    ("tick", "max_turns_ceiling"): "AIFORGE_TICK_MAX_TURNS_CEILING",
    ("api", "base"):               "AIFORGE_API_BASE",
    ("intent", "enrich"):          "AIFORGE_INTENT_ENRICH",
    ("intent", "lm_url"):          "AIFORGE_INTENT_LM_URL",
    ("codemem", "query_enabled"):  "AIFORGE_CODEMEM_QUERY",
    ("neo4j", "uri"):              "AIFORGE_NEO4J_URI",
    ("neo4j", "user"):             "AIFORGE_NEO4J_USER",
    ("neo4j", "password"):         "AIFORGE_NEO4J_PASSWORD",
}


def _walk(obj: dict, path: tuple[str, ...]):
    cur: object = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def load(path: str | Path | None = None) -> dict:
    """Read yaml + export to env (env wins). Returns parsed dict."""
    if yaml is None:
        return {}
    p = Path(path or _DEFAULT_PATH)
    if not p.is_file():
        return {}
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError:
        return {}

    for keys, env_key in _FIELD_MAP.items():
        v = _walk(data, keys)
        if v is None:
            continue
        # Booleans → "1"/"0", everything else → str.
        if isinstance(v, bool):
            v_str = "1" if v else "0"
        else:
            v_str = str(v)
        # Env wins.
        if env_key not in os.environ:
            os.environ[env_key] = v_str
    return data


# Auto-load at import unless disabled
if os.environ.get("AIFORGE_CONFIG_AUTOLOAD", "1") != "0":
    load()
