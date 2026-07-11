"""Structured LLM output — Pydantic-validated completion with reask retry.

Two paths, best available wins (``AIFORGE_STRUCTURED_MODE`` = ``auto`` |
``instructor`` | ``fallback``):

1. **instructor** (optional extra ``structured``): wraps the role's resolved
   OpenAI-compatible endpoint with ``Mode.MD_JSON`` — schema-in-prompt + JSON
   extraction + validation + automatic reask. MD_JSON works on local servers
   (LM Studio) that reject ``response_format: json_object``.
2. **fallback** (always available, zero extra deps): the same
   schema-prompt → extract → validate → reask loop hand-rolled over
   :func:`aiforge_core.llm.client.complete` — which keeps the FULL
   EscalatingLlm retry/escalation chain (the instructor path talks to the
   primary endpoint directly, so any instructor failure falls through here).

Both return a validated ``response_model`` instance or raise ``ValueError``
after exhausting retries. Replaces the hand-rolled ``re.search(r"{.*}")``
scraping at the planner/architect seams.
"""
from __future__ import annotations

import json
import logging
import os

from pydantic import BaseModel, ValidationError

log = logging.getLogger("aiforge.llm.structured")


def _mode() -> str:
    m = os.environ.get("AIFORGE_STRUCTURED_MODE", "auto").strip().lower()
    return m if m in ("auto", "instructor", "fallback") else "auto"


def extract_json(text: str) -> str | None:
    """Best-effort JSON object/array extraction from a model reply: strips a
    ```json fence if present, else takes the first ``{``/``[`` to its matching
    last ``}``/``]``. Returns None when nothing JSON-shaped is found."""
    t = (text or "").strip()
    if not t:
        return None
    if "```" in t:
        # Take the FIRST fenced block that looks like JSON. Guard against a
        # stray/empty fence: `"" in "{["` is True, so a bare `inner[:1] in`
        # check used to clobber `t` to "" when the model appended a lone ```
        # after its JSON. Scan every block; fall through to the raw text when
        # none is JSON-shaped.
        for p in t.split("```")[1:]:
            inner = p
            if inner.lower().lstrip().startswith("json"):
                inner = inner.lstrip()[4:]
            inner = inner.strip()
            if inner and inner[0] in "{[":
                t = inner
                break
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = t.find(open_ch)
        end = t.rfind(close_ch)
        if 0 <= start < end:
            return t[start:end + 1]
    return None


def _schema_instruction(response_model: type[BaseModel]) -> str:
    return (
        "Respond with ONLY a single JSON object that validates against this "
        "JSON Schema — no prose, no markdown fence, no comments:\n"
        + json.dumps(response_model.model_json_schema(), ensure_ascii=False)
    )


def _fallback_complete(role: str, messages: list[dict],
                       response_model: type[BaseModel], *,
                       max_retries: int, max_tokens: int | None,
                       timeout_s: int | None,
                       temperature: float | None = None) -> BaseModel:
    """Schema-prompt + extract + validate + reask over client.complete()
    (keeps routing/escalation). Raises ValueError when retries exhaust."""
    from aiforge_core.llm import client
    msgs = [dict(m) for m in messages]
    instr = _schema_instruction(response_model)
    if msgs and msgs[0].get("role") == "system":
        msgs[0]["content"] = (msgs[0].get("content") or "") + "\n\n" + instr
    else:
        msgs.insert(0, {"role": "system", "content": instr})
    last_err: Exception | None = None
    for attempt in range(max(1, max_retries + 1)):
        out = client.complete(role, msgs, max_tokens=max_tokens,
                              timeout_s=timeout_s, temperature=temperature)
        raw = extract_json(out) or (out or "").strip()
        try:
            return response_model.model_validate_json(raw)
        except ValidationError as exc:
            last_err = exc
        except Exception as exc:  # noqa: BLE001 — e.g. not JSON at all
            last_err = exc
        # Reask (instructor-style): show the model its own reply + the
        # validation error, demand corrected JSON only.
        msgs.append({"role": "assistant", "content": (out or "")[:4000]})
        msgs.append({"role": "user", "content":
                     f"Your reply did not validate: {str(last_err)[:800]}\n"
                     "Resend ONLY the corrected JSON object — nothing else."})
        log.debug("structured fallback reask %d/%d role=%s err=%s",
                  attempt + 1, max_retries, role, last_err)
    raise ValueError(f"structured output failed after {max_retries + 1} "
                     f"attempts (role={role}): {last_err}")


def structured_complete(role: str, messages: list[dict],
                        response_model: type[BaseModel], *,
                        max_retries: int = 2,
                        max_tokens: int | None = None,
                        timeout_s: int | None = None,
                        temperature: float | None = None) -> BaseModel:
    """One structured completion for ``role`` → validated ``response_model``.

    Tries the instructor ADAPTER when installed (unless mode=fallback);
    ANY adapter error falls through to the self-contained loop over
    ``client.complete`` so escalation/fallback routing still applies.
    Raises ``ValueError`` when both paths exhaust."""
    mode = _mode()
    if mode in ("auto", "instructor"):
        from aiforge_core.integrations import instructor_adapter
        if instructor_adapter.available():
            try:
                from aiforge_core.llm.client import resolve
                ep = resolve(role)
                res = instructor_adapter.structured(
                    base_url=ep.base_url, api_key=ep.api_key, model=ep.model,
                    messages=messages, response_model=response_model,
                    max_retries=max_retries, max_tokens=max_tokens,
                    timeout_s=timeout_s, temperature=temperature)
                # The instructor path talks to the endpoint directly (bypasses
                # client.complete), so mirror it to Langfuse here too.
                try:
                    from aiforge_core.llm.client import _trace_generation
                    _trace_generation(role, list(messages),
                                      res.model_dump_json()[:8000], 0)
                except Exception:  # noqa: BLE001 — tracing never breaks a call
                    pass
                return res
            except Exception as exc:  # noqa: BLE001 — fall back to our loop
                log.info("instructor path failed [role=%s base_url=%s]: %s: %s "
                         "— using fallback loop", role,
                         getattr(ep, "base_url", "?"), type(exc).__name__,
                         str(exc)[:200])
        elif mode == "instructor":
            raise ImportError(
                "AIFORGE_STRUCTURED_MODE=instructor but the lib is not "
                "installed — pip install 'aiforgecrew[structured]'")
    return _fallback_complete(role, messages, response_model,
                              max_retries=max_retries, max_tokens=max_tokens,
                              timeout_s=timeout_s, temperature=temperature)


__all__ = ["structured_complete", "extract_json"]
