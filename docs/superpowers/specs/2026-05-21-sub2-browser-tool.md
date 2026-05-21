# Sub #2 — Browser Tool (OpenHands parity)

**Date:** 2026-05-21
**Depends on:** Sub #1 (tools/ package scaffold)

## Goal

OH-parity headless browser. Tool: `browse(command, **kwargs)`.

## Module

`aiforge_core/runtime/tools/browser.py` — single dispatcher.

## Commands

| command | kwargs | returns |
|---|---|---|
| `goto` | `url` | `{ok, url, title, status}` |
| `screenshot` | `path` (optional, relative inside repo) | `{ok, path, png_b64}` |
| `click` | `selector` | `{ok}` |
| `fill` | `selector`, `text` | `{ok}` |
| `extract_text` | `selector` (optional, default body) | `{ok, text}` |
| `close` | — | `{ok}` |

## Implementation

- One headless Playwright `BrowserContext` per ADK run_id. Lazy create. Lifecycle mirrors bash session.
- Sync Playwright API (avoids ADK async leak).
- URL allowlist via `AIFORGE_BROWSER_ALLOWLIST` env (CSV regexes). Empty = allow all (dev). Production sets `^https?://` only.
- Body cap 32 KB per `extract_text`, screenshot 256 KB.
- Soft-error contract: `{ok: False, error, ...}`.
- Fallback: playwright not installed → `{ok: False, error: "playwright_missing"}`.
- `:Browse` trace event per call.

## Tests

- mock Playwright via context-manager stub (no real network in tests)
- happy goto / screenshot / click / fill / extract_text / close
- not-installed fallback
- URL allowlist enforcement

## Agents.yaml

Doer gets `browser` in allowed (separate from `bash` per separation principle).
