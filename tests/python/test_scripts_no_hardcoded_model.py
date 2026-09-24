"""No hard-coded model ids on the runtime path — model comes from config/env.

A baked-in model id made the box load a second large model next to (or
instead of) the one the operator configured. With no model set, each script
must say so and skip the load rather than guess one.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MEMORY_PKG = ROOT / "packages/aiforge_memory/aiforge_memory"

# Model-family names that only appear in a model id. ``llama`` is anchored so
# ``ollama`` (a server, not a model) does not match.
MODEL_ID_RE = re.compile(
    r"qwen|(?<![a-z])llama|gemma|gpt-oss|nex-n|mistral|deepseek|claude-"
    r"|gpt-[45]", re.IGNORECASE)

# Documented capability / pricing tables: they CLASSIFY a model the operator
# already chose, they never pick one.
TABLES = {ROOT / "aiforge_core/runtime/vision.py"}

SHELL_SCRIPTS = sorted([*(ROOT / "scripts/runtime").rglob("*.sh"),
                        *MEMORY_PKG.rglob("*.sh")])
PY_FILES = sorted(
    p for base in (ROOT / "aiforge_core/runtime",
                   ROOT / "aiforge_core/orchestrator", MEMORY_PKG)
    for p in base.rglob("*.py")
    if "tests" not in p.relative_to(base).parts and p not in TABLES)

LMS_ENSURE = ROOT / "scripts/runtime/nuc/lms-ensure.sh"


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=_rel)
def test_no_model_id_in_a_script(script):
    hits = [f"{i}: {line.strip()}"
            for i, line in enumerate(script.read_text().splitlines(), 1)
            if MODEL_ID_RE.search(line)]
    assert not hits, hits


def _docstrings(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)):
            out.add(id(body[0].value))
    return out


def test_no_model_id_in_a_python_string_literal():
    """String literals only (docstrings/comments may name a model as an
    example): a literal is what becomes a default."""
    hits = []
    for p in PY_FILES:
        tree = ast.parse(p.read_text())
        docs = _docstrings(tree)
        hits += [f"{_rel(p)}:{n.lineno}: {n.value[:60]!r}"
                 for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and id(n) not in docs and MODEL_ID_RE.search(n.value)]
    assert not hits, hits


def test_the_scan_sees_what_it_should():
    """Guard the guard: an empty file list would pass vacuously."""
    assert LMS_ENSURE in SHELL_SCRIPTS
    assert ROOT / "aiforge_core/runtime/pr_reviewer.py" in PY_FILES
    assert MEMORY_PKG / "features/symbol/summarise.py" in PY_FILES
    assert MODEL_ID_RE.search("openai/Qwen3-Coder-Next")
    assert not MODEL_ID_RE.search("ollama (public)")


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=_rel)
def test_script_parses(script):
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


def test_lms_ensure_skips_loudly_when_no_model_is_configured(tmp_path):
    """Host set, no model → ERROR on stderr, exit 0, and no ssh at all."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("AIFORGE_LMS_")}
    env.update(AIFORGE_LMS_HOST="nobody@invalid.invalid",
               AIFORGE_CONFIG_DIR=str(tmp_path),
               PATH=str(tmp_path) + os.pathsep + env.get("PATH", ""))
    # A fake ssh that records any call — the skip must happen before it.
    fake = tmp_path / "ssh"
    fake.write_text(f"#!/bin/sh\ntouch {tmp_path}/ssh-called\nexit 1\n")
    fake.chmod(0o755)

    out = subprocess.run(["bash", str(LMS_ENSURE)], env=env,
                         capture_output=True, text=True, timeout=30)

    assert out.returncode == 0, out.stderr
    assert "ERROR" in out.stderr and "no model configured" in out.stderr
    assert not (tmp_path / "ssh-called").exists()


def test_wire_judge_model_refuses_without_a_model(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "JUDGE_MODEL"}
    env["AIFORGE_PY"] = str(tmp_path / "no-python")   # must never be reached
    out = subprocess.run(
        ["bash", str(ROOT / "scripts/runtime/nuc/wire-judge-model.sh")],
        env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 1
    assert "JUDGE_MODEL unset" in out.stderr


def test_kv_quant_config_skips_without_a_model(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "KV_MODELS"}
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")
    fake = tmp_path / "ssh"
    fake.write_text(f"#!/bin/sh\ntouch {tmp_path}/ssh-called\nexit 1\n")
    fake.chmod(0o755)
    out = subprocess.run(
        ["bash", str(ROOT / "scripts/runtime/nuc/lms-kv-quant-config.sh")],
        env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    assert "ERROR" in out.stderr and "KV_MODELS unset" in out.stderr
    assert not (tmp_path / "ssh-called").exists()
