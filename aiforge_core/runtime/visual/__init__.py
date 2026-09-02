"""Visual verification: turn a running UI into text an agent can reason about.

Public surface:

* :func:`ui_check` — serve-or-reuse → navigate → capture → audit, one call.
* :func:`ui_ask` — a follow-up question about a capture ``ui_check`` stored.
* :func:`audit_image` / :func:`ask_image` — the pixels→text step on its own.
* :func:`vision_role` — which role can see, or the reason none can.
"""
from __future__ import annotations

from ._ask import ui_ask
from ._audit import ask_image, audit_image
from ._captures import capture_path, captures_dir, save_capture
from ._macro import ui_check
from ._role import vision_role

__all__ = ["ui_check", "ui_ask", "audit_image", "ask_image", "vision_role",
           "save_capture", "capture_path", "captures_dir"]
