# Sub #5 — Microagents

**Date:** 2026-05-21
**Depends on:** none

## Goal

OH-parity microagent triggers — frontmatter-tagged markdown files that inject context when keywords appear in the prompt or tool output.

## Module

`aiforge_core/runtime/microagents.py`

## File format

`~/.aiforge/microagents/*.md`:

```markdown
---
name: pytest-tips
type: knowledge
triggers: [pytest, fixture, conftest]
priority: 5
---

# Pytest patterns

When using pytest fixtures, ...
```

Frontmatter fields:
- `name` — unique slug
- `type` — `knowledge` | `repo` | `task`
- `triggers` — list of keyword strings (case-insensitive substring)
- `priority` — int, higher = injected earlier

## API

```python
def load_microagents(dir: Path | None = None) -> list[Microagent]
def match(text: str, agents: list[Microagent]) -> list[Microagent]
def render_injection(matches: list[Microagent]) -> str
```

## Integration

Pipeline pre-prompt hook calls `match` against ticket body + last tool output; prepends `render_injection` to next agent prompt.

## Tests

- frontmatter parse: name/type/triggers/priority
- match: text containing one trigger returns matching agent
- match: priority sort
- render_injection: concatenates body w/ `<microagent name=...>` delimiters
- empty / missing dir → empty list, no raise
