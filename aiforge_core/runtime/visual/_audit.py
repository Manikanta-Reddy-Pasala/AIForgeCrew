"""Screenshot → text. The only place pixels become words.

Two shapes, both one LLM call on a vision role:

* :func:`audit_image` — a fixed-schema UI audit. The schema is the point: the
  agent that asked for a mock screen does not know what is wrong with it, so a
  free-form caption ("a login form on a white background") answers a question
  nobody had. A fixed ISSUES list makes the model look for overlap, clipping
  and error text every time, including the times the agent had no suspicion.
* :func:`ask_image` — one targeted question about a stored capture, for when
  the audit has already pointed somewhere and the agent needs detail.
"""
from __future__ import annotations

import os
from typing import Any

from ._role import vision_role

_AUDIT_PROMPT = (
    "You are inspecting a screenshot of a running application's UI. Report ONLY "
    "what is visible in the image — never guess at code, data or intent.\n\n"
    "Reply in exactly this shape:\n"
    "SCREEN: <1-2 sentences: what page this is and its main regions>\n"
    "ISSUES:\n"
    "- <one line per VISIBLE defect>\n"
    "TEXT: <the visible text in reading order, condensed>\n\n"
    "Count as an issue: elements overlapping or clipped; text cut off, "
    "truncated or unreadable; a blank/white/black region where content belongs; "
    "a broken or missing image; text whose contrast makes it hard to read; "
    "controls pushed off-screen or misaligned; visible error text, stack traces "
    "or placeholder strings (undefined, NaN, null, lorem ipsum, TODO); a layout "
    "that is obviously not the intended one. If the screen looks correct, write "
    "exactly '- none'."
)

_ASK_PREFIX = (
    "Answer strictly from what is visible in this screenshot, in at most three "
    "sentences. If the image does not show enough to answer, say so plainly. "
    "Give the answer itself — no reasoning, no preamble.\n\nQuestion: ")

_DEFAULT_MAX_TOKENS = 1200


def _max_tokens() -> int:
    """Generous by default: the local VLM on this stack is a THINKING model
    whose reasoning is billed against the same budget, and a small cap returns
    an empty ``content`` with the whole allowance spent on reasoning."""
    try:
        return max(120, int(os.environ.get(
            "AIFORGE_UI_AUDIT_MAX_TOKENS", _DEFAULT_MAX_TOKENS)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TOKENS


def _run(path: str, prompt: str, role: str,
         max_tokens: int | None = None) -> dict[str, Any]:
    vrole, reason = vision_role(role)
    if not vrole:
        return {"ok": False, "error": "no_vision_model", "hint": reason}
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "image_not_found", "path": path,
                "hint": f"no image at {path} — re-run ui_check"}

    from aiforge_core.runtime import vision as _vision
    content = _vision.attach_image(prompt, path)
    if isinstance(content, dict):        # soft error from attach_image
        # Every failure branch carries a hint. A caller that renders
        # `hint or "no vision model"` would otherwise tell the operator to go
        # configure a VLM that is already configured and working.
        err = content.get("error") or "image_unreadable"
        hints = {
            "image_too_large": ("the capture exceeds the vision input limit — "
                                "drop full_page, or narrow the viewport"),
            "unsupported_format": "the capture is not a readable PNG/JPEG",
            "not_found": f"no image at {path} — re-run ui_check",
        }
        return {"ok": False, "error": err, "path": path,
                "hint": hints.get(err, f"the image at {path} could not be read "
                                       f"({err})")}
    try:
        # complete_raw, not complete: the shared text extractor falls back to
        # the ``reasoning_content`` channel when ``content`` is empty, which on
        # a THINKING VLM hands back raw chain-of-thought ("The user wants a
        # yes/no answer… Let me look carefully") as if it were the audit. Read
        # ``content`` ourselves so an answerless reply is reported as one.
        from aiforge_core.llm.client import complete_raw
        from aiforge_core.llm.client._text import _strip_think
        msg = complete_raw(vrole, [{"role": "user", "content": content}],
                           max_tokens=max_tokens or _max_tokens())
    except Exception as exc:  # noqa: BLE001 — a dead VLM must not kill the turn
        return {"ok": False, "error": "vision_call_failed",
                "detail": str(exc)[:300], "vision_role": vrole}
    text = _strip_think(str((msg or {}).get("content") or "").strip())
    if not text:
        return {"ok": False, "error": "vision_empty_reply", "vision_role": vrole,
                "hint": ("the vision model spent its whole budget reasoning and "
                         "answered nothing — raise AIFORGE_UI_AUDIT_MAX_TOKENS "
                         f"(currently {max_tokens or _max_tokens()})")}
    return {"ok": True, "text": text, "vision_role": vrole}


def audit_image(path: str, *, role: str = "chat",
                max_tokens: int | None = None) -> dict[str, Any]:
    """Fixed-schema UI audit of the image at ``path``."""
    return _run(path, _AUDIT_PROMPT, role, max_tokens)


def ask_image(path: str, question: str, *, role: str = "chat",
              max_tokens: int | None = None) -> dict[str, Any]:
    """Answer one question about the image at ``path``."""
    q = (question or "").strip()
    if not q:
        return {"ok": False, "error": "missing_question"}
    return _run(path, _ASK_PREFIX + q, role, max_tokens)


__all__ = ["audit_image", "ask_image"]
