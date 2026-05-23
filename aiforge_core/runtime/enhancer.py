"""Claude-side ticket Enhancer (new pre-flight stage).

Runs ONCE per ticket *before* the SequentialAgent pipeline starts.
Uses the operator's raw title + body + memory hits to produce a
richer brief that the local Doer (qwen-coder-next via LM Studio)
can act on without ambiguity.

Why a separate module instead of an ADK LlmAgent stage:

* Synchronous: we want the enhanced body to land in
  ``ticket.metadata["enhanced_body"]`` and in the prompt fed to the
  Planner — that's easier as a plain function call than weaving a
  new SequentialAgent slot.
* Provider-pinned: Enhancer is always Claude. The agent allowlist /
  EscalatingLlm machinery would just get in the way.
* Failure-soft: when Claude is unreachable or returns
  ``ENHANCE_BLOCKED``, we fall through with the raw body so the
  pipeline still runs.

Toggle via ``AIFORGE_ENHANCER=0`` (default on).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess

from aiforge_core.runtime.prompts import ENHANCER

log = logging.getLogger("aiforge.enhancer")

_BLOCK_PREFIX = "ENHANCE_BLOCKED:"


def _claude_cli_invoke(prompt: str, *, timeout: int = 180) -> str | None:
    """Call the ``claude`` subscription CLI in batch mode.

    Returns the stdout text on success, ``None`` on any failure
    (missing binary, non-zero exit, empty output). Pinned to the
    Opus model — Enhancer needs the smartest brief-rewriter we have.
    """
    cli = shutil.which("claude") or os.environ.get("AIFORGE_CLAUDE_BIN")
    if not cli or not shutil.which(cli):
        return None
    model = os.environ.get("AIFORGE_ENHANCER_MODEL", "claude-opus-4-7")
    fallback = os.environ.get(
        "AIFORGE_ENHANCER_FALLBACK", "claude-sonnet-4-6",
    )
    cmd = [
        cli, "--print", "--permission-mode", "bypassPermissions",
        "--model", model, "--fallback-model", fallback,
    ]
    try:
        p = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("enhancer timeout after %ds", timeout)
        return None
    if p.returncode != 0:
        log.warning("enhancer cli rc=%s stderr=%s",
                    p.returncode, p.stderr[-300:])
        return None
    out = (p.stdout or "").strip()
    return out or None


def _build_prompt(ticket, memory_md: str) -> str:
    body = ticket.body or "(no body)"
    repo = ticket.project or "(unset)"
    md = ticket.metadata or {}
    ext_refs = md.get("external_refs") or []
    parts = [
        ENHANCER,
        "",
        "## Operator-supplied ticket",
        f"### Title\n{ticket.title}",
        f"### Body\n{body}",
        f"### Target repo\n{repo}",
    ]
    if ext_refs:
        parts.append("### External references")
        parts += [f"- {r}" for r in ext_refs[:10]]
    if memory_md:
        parts.append("")
        parts.append(memory_md.strip())
    return "\n\n".join(parts)


def enhance(ticket, memory_md: str = "") -> dict:
    """Return ``{ok, enhanced_body?, blocked_reason?, used_claude}``.

    The runner stitches the enhanced body into the Planner's seed
    prompt; the raw ``ticket.body`` is preserved on disk so audits
    + replays still see what the operator wrote.
    """
    out: dict = {"ok": False, "used_claude": False}
    if os.environ.get("AIFORGE_ENHANCER", "1") in {"0", "false", ""}:
        out["error"] = "disabled"
        return out
    prompt = _build_prompt(ticket, memory_md or "")
    text = _claude_cli_invoke(prompt)
    if text is None:
        out["error"] = "claude_unreachable_or_failed"
        return out
    out["used_claude"] = True
    if text.startswith(_BLOCK_PREFIX):
        out["blocked_reason"] = text[len(_BLOCK_PREFIX):].strip()[:400]
        return out
    # Trim accidental fences / leading verbiage.
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    out["ok"] = True
    out["enhanced_body"] = text.strip()
    return out


__all__ = ["enhance"]
