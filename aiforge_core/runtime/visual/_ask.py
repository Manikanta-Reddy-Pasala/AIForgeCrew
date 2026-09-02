"""``ui_ask`` — a follow-up question about a capture already taken.

The audit is deliberately fixed-shape, which makes it good at finding issues
and bad at answering "what exactly does the third row say". Re-capturing to
ask that would re-run the server, the navigation and the settle for an image
that is already on disk, and would answer about a DIFFERENT render.
"""
from __future__ import annotations

from typing import Any

from ._audit import ask_image, audit_image
from ._captures import capture_path


def ui_ask(args: dict, _cwd: str | None = None) -> dict[str, Any]:
    """Ask the vision model about a stored capture. Never raises."""
    capture_id = str(args.get("capture_id") or "").strip()
    if not capture_id:
        return {"ok": False, "error": "missing_capture_id",
                "hint": "pass the capture_id returned by ui_check"}
    path = capture_path(capture_id)
    if path is None:
        return {"ok": False, "error": "capture_not_found",
                "capture_id": capture_id,
                "hint": "captures are pruned over time — re-run ui_check"}
    question = str(args.get("question") or "").strip()
    role = str(args.get("role") or "chat")
    result = (ask_image(path, question, role=role) if question
              else audit_image(path, role=role))
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"),
                "hint": result.get("hint") or result.get("detail"),
                "capture_id": capture_id, "screenshot": path}
    return {"ok": True, "capture_id": capture_id, "screenshot": path,
            "answer": result["text"], "vision_role": result.get("vision_role")}


__all__ = ["ui_ask"]
