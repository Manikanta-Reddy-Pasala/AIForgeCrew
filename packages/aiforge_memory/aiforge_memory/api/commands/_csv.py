"""Tiny ``--tags a,b,c`` -> ``["a","b","c"]`` helper.

Trims whitespace + drops empty entries so ``"a,, b,"`` round-trips to
``["a","b"]`` without surprises.
"""
from __future__ import annotations


def split_csv(s: str | None) -> list[str]:
    if not s:
        return []
    return [t.strip() for t in s.split(",") if t.strip()]


__all__ = ["split_csv"]
