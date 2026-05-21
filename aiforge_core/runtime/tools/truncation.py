"""OH-parity TruncatedObservation marker (sub #13).

Single helper :func:`mark_truncated` that wraps a possibly-capped string
with an explicit ``<truncated bytes_dropped=N>`` suffix when the cap
was hit. Lets the model see WHEN output was cut off vs receiving a
silently shortened block.

Usage::

    body = mark_truncated(raw, _STDOUT_CAP_BYTES)
"""
from __future__ import annotations

_SUFFIX_FMT = "\n<truncated bytes_dropped={n}>"


def mark_truncated(value: str, cap_bytes: int) -> tuple[str, bool]:
    """Return ``(possibly-marked-string, was_truncated)``.

    No allocation when ``value`` fits within ``cap_bytes``.
    """
    if not isinstance(value, str):
        return value, False
    raw = value.encode("utf-8")
    if len(raw) <= cap_bytes:
        return value, False
    dropped = len(raw) - cap_bytes
    head = raw[:cap_bytes].decode("utf-8", "replace")
    return head + _SUFFIX_FMT.format(n=dropped), True


__all__ = ["mark_truncated"]
