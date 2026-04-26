"""Per-repo defaults loader.

Reads ``.aiforge/aiforge.conf.yml`` from the worktree (Aider's
``.aider.conf.yml`` analogue, our naming) and returns a dict of
overrides:

    lint_cmd: 'mvn -DskipTests checkstyle:check'
    test_cmd: 'mvn test -Pfast'
    primary_backend: gemini
    max_turns: 30
    readonly:
      - src/main/java/com/oneshell/commons/**

Values feed into:
- ``lint.handle`` / ``tests.handle`` (lint_cmd / test_cmd)
- ``llm_picker.resolve`` via env layering (primary_backend)
- ``readonly.collect`` (readonly list)
- ``run_doer_via_ga`` max_turns

Reads YAML via stdlib-compatible ``yaml`` if available; else
KISS line parser for the simple top-level keys we use.
"""
from __future__ import annotations

import os


def load(worktree: str) -> dict:
    path = os.path.join(worktree, ".aiforge", "aiforge.conf.yml")
    if not os.path.isfile(path):
        return {}
    text = open(path, "r", encoding="utf-8", errors="replace").read()
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            return {}
        return data
    except ImportError:
        return _kiss_parse(text)


def _kiss_parse(text: str) -> dict:
    """Tiny YAML-ish parser for top-level scalar keys + list under
    ``readonly:``. Doesn't handle nested maps; suffices for our
    flat config schema.
    """
    out: dict = {}
    cur_list_key: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("- ") and cur_list_key is not None:
            out.setdefault(cur_list_key, []).append(line[2:].strip())
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip("'\"")
            if val:
                out[key] = val
                cur_list_key = None
            else:
                cur_list_key = key
    return out


def apply_to_env(cfg: dict) -> None:
    """Lift selected config values into env so other tools pick up.

    Idempotent — only sets when env not already set so explicit
    operator overrides win. Call once at doer start.
    """
    if cfg.get("lint_cmd") and "AIFORGE_DOER_LINT_CMD" not in os.environ:
        os.environ["AIFORGE_DOER_LINT_CMD"] = str(cfg["lint_cmd"])
    if cfg.get("test_cmd") and "AIFORGE_DOER_TEST_CMD" not in os.environ:
        os.environ["AIFORGE_DOER_TEST_CMD"] = str(cfg["test_cmd"])
    if cfg.get("primary_backend") and "AIFORGE_PRIMARY_BACKEND" not in os.environ:
        os.environ["AIFORGE_PRIMARY_BACKEND"] = str(cfg["primary_backend"])
