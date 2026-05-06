"""Cheap syntax sniff used by ``file_write`` to reject obvious LLM
hallucinations before they hit disk.

Goals: catch the model's most common failure modes — half-truncated
output (unbalanced braces), Python parse errors, Java/Kotlin code that
looks like the model lapsed back into Python kwargs syntax mid-method.

Keep it intentionally cheap. We can't afford a full per-language parser
inside the agent loop — that's what the post-write ``run_shell`` mvn /
pytest call is for. This guard only blocks the *clearly* corrupt drafts
so the Doer's next turn sees a useful error string instead of a green
file_write that quietly broke the build later.
"""
from __future__ import annotations

import re

_PAIRS: tuple[tuple[str, str], ...] = (("{", "}"), ("(", ")"), ("[", "]"))
_KWARG_PATTERN = re.compile(r"\b\w+\s*=\s*\w+[\s,]")
_ANNOTATION_PATTERN = re.compile(r"@\w+\s*\(")


def validate_syntax(path: str, content: str) -> tuple[bool, str]:
    """Return ``(ok, error_msg)``. Empty error string on the happy path.

    Triggers:

    * empty / whitespace-only content → reject
    * any of ``{}``, ``()``, ``[]`` not balanced → reject
    * .py — ``compile()`` raised → reject with line number
    * .java / .kt — ``foo = bar`` inside parens AND no annotation
      context (``@Bean(name = "x")`` is fine) → reject

    Other extensions only get the brace-balance check.
    """
    if not content or not content.strip():
        return False, "empty file content"

    for opener, closer in _PAIRS:
        n_open = content.count(opener)
        n_close = content.count(closer)
        if n_open != n_close:
            return False, (
                f"unbalanced {opener}{closer} ({n_open} vs {n_close})"
            )

    if path.endswith(".py"):
        try:
            compile(content, path, "exec")
        except SyntaxError as exc:
            return False, f"python syntax: {exc.msg} at line {exc.lineno}"

    if path.endswith((".java", ".kt")):
        if _KWARG_PATTERN.search(content) and "(" in content:
            if not _ANNOTATION_PATTERN.search(content):
                return False, (
                    "java/kotlin: looks like Python-style kwargs in call"
                )

    return True, ""


__all__ = ["validate_syntax"]
