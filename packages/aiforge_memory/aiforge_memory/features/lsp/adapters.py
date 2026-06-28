"""Per-language LSP server launchers.

Maps tree-sitter ``WalkedFile.lang`` -> (executable list[str], language_id).
``adapter_for(lang)`` returns ``None`` when no server is configured or
the binary is not on PATH — caller falls back to tree-sitter heuristics.

Adding a new language: register in ``_ADAPTERS``. Each entry is a
callable returning (cmd, lang_id, init_options) or ``None``.

Maturity:
    python  — supported (pyright-langserver)
    typescript / tsx / javascript — supported (typescript-language-server)
    java    — experimental (jdtls; needs a -data dir; left to operator)
"""
from __future__ import annotations

import os
import shutil


def adapter_for(lang: str) -> tuple[list[str], str, dict] | None:
    """Return (cmd, language_id, init_options) for the given lang, or
    None when no adapter is registered or the binary is missing."""
    factory = _ADAPTERS.get(lang)
    if factory is None:
        return None
    return factory()


def available_servers() -> dict[str, bool]:
    """Operator-facing: which adapters are usable on this host."""
    out: dict[str, bool] = {}
    for lang, factory in _ADAPTERS.items():
        try:
            out[lang] = factory() is not None
        except Exception:  # noqa: BLE001
            out[lang] = False
    return out


# ─── Per-language adapters ────────────────────────────────────────────

def _python() -> tuple[list[str], str, dict] | None:
    """Prefer pyright-langserver; fall back to pylsp."""
    if shutil.which("pyright-langserver"):
        return (["pyright-langserver", "--stdio"], "python", {})
    if shutil.which("pylsp"):
        return (["pylsp"], "python", {})
    return None


def _typescript() -> tuple[list[str], str, dict] | None:
    bin_name = "typescript-language-server"
    if not shutil.which(bin_name):
        return None
    return ([bin_name, "--stdio"], "typescript", {})


def _javascript() -> tuple[list[str], str, dict] | None:
    bin_name = "typescript-language-server"
    if not shutil.which(bin_name):
        return None
    return ([bin_name, "--stdio"], "javascript", {})


def _java() -> tuple[list[str], str, dict] | None:
    """jdtls launcher — operator must set AIFORGE_JDTLS_CMD to a launch
    script (jdtls needs a -data dir + workspace dir, varies by install)."""
    cmd = os.environ.get("AIFORGE_JDTLS_CMD", "")
    if not cmd:
        return None
    parts = cmd.split()
    if not shutil.which(parts[0]):
        return None
    return (parts, "java", {})


_ADAPTERS = {
    "python":     _python,
    "typescript": _typescript,
    "tsx":        _typescript,
    "javascript": _javascript,
    "java":       _java,
}
