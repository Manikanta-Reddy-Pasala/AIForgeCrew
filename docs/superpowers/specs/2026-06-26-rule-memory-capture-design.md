# Rule / Memory / Feedback Capture — Design

**Date:** 2026-06-26
**Status:** Approved (design), pending implementation plan

## Problem

When a user types a chat message that is actually a directive, fact, or
correction — e.g. *"for git commit, commit directly because the machine has
access"* — the system should:

1. **Detect** deterministically that the message carries something to remember.
2. **Classify** it: Rule vs Memory vs Feedback.
3. **Scope** it: global (all repos/sessions), project (this repo), or session
   (this conversation only).
4. **Store** it in the right place.
5. **Apply** it next time — never re-ask the same question (the commit example
   must stop the approval gate from re-prompting).
6. **Tell the user** where it was stored, and let them correct the scope or undo.

### Today (the gap)

- `_t_remember_rule` (chat_agent.py) — a **tool the model may call**; stores a
  rule at scope `global` or `repo` in `md_store`; re-injected via
  `_rules_context`.
- `_t_memory_write` — durable facts.
- Prompt trigger (chat_agent.py ~1042): *"when the user says remember/always/
  never/for X…"*.

Gaps: **model-driven (not deterministic** — weak local models miss it);
**no session scope**; **no Rule/Memory/Feedback classifier**; **no
transparency/undo**; rules are injected but the **approval gates don't honor
them** (so "always commit directly" still pops the gate).

## Decisions (from brainstorming)

- **Detection:** always-on classifier pass (every user chat message).
- **Confirmation:** auto-store at the classified scope + an inline correctable
  note (change-scope / undo). No blocking confirm.
- **Categories:** Rule, Memory, Feedback — each with its own store.
- **Scopes:** global / project / session.

## Architecture

### Flow

```
user message
   │
   ▼
rule_capture.classify(message, repo, session_id)   ← small capped LLM call
   │  → {category, scope, canonical, confidence}
   ▼
category != "none" AND confidence ≥ threshold?
   │ yes                                   │ no
   ▼                                        ▼
store + apply_behavioral + emit `captured`  (skip)
   │
   ▼
continue normal agent run
   (message may ALSO be a task → do it;
    a PURE rule/memory short-circuits with a brief ack)
```

The classifier runs in the chat message handler (`api.chat_session_message`)
**before** the agent, so capture is deterministic and independent of which
model the agent uses. It is a SEPARATE concern from the enhancer (capture
first, then enhance+run on the residual task).

### Components (isolated, independently testable)

- **`aiforge_core/runtime/rule_capture.py`**
  - `classify(message, *, repo, session_id) -> Classification` — one capped LLM
    call (env `AIFORGE_RULE_CLASSIFY_TIMEOUT_S` default 15, low max_tokens),
    strict-JSON parse with defensive fallback to `category="none"` on any
    parse/LLM error. Pure aside from the LLM call → testable with a mocked
    `complete`.
    - `Classification = {category: "rule"|"memory"|"feedback"|"none",
      scope: "global"|"project"|"session", canonical: str, confidence: float}`
  - `store(c, *, repo, session_id) -> Stored{id, location}` — routes by
    category × scope (table below). Never raises.
  - `apply_behavioral(c) -> list[str]` — maps RECOGNIZED rule patterns to gate
    flags (e.g. auto-approve commits / deletes for this scope). Returns the
    list of flags applied (for the UI note). Unrecognized rules → no flag, just
    injected guidance.
  - `list_captured(repo, session_id)`, `rescope(id, new_scope)`,
    `undo(id)` — for the transparency UI.
- **Chat handler** (`api.chat_session_message`): pre-agent classify→store→emit;
  PURE-capture short-circuit (brief ack, no agent run).
- **SSE event** `{"type":"captured", "id", "category", "scope", "text",
  "flags"}`.
- **API endpoints:** `GET /api/rules` (list per scope), `PUT /api/rules/{id}/scope`,
  `DELETE /api/rules/{id}`.
- **Chat.tsx:** the inline `captured` pill (change-scope ▾ / undo) + a "Rules &
  memories" panel (view/edit/delete per scope).

### Storage (category × scope)

| Category | global | project | session |
|---|---|---|---|
| **Rule** | `md_store source="rules:global"` (injected every session via `_rules_context`) | `md_store source="rules:{repo}"` **+** write `<repo>/.aiforge/rules/<slug>.md` so the ticket/doer `repo_rules` honors it too | in-session store (chat session state); injected into THIS session only; not persisted |
| **Memory** | AiForgeMemory `Note_v2`/`Observation_v2`, scope tag `global` | same, repo-tagged | session note (ephemeral) |
| **Feedback** | as Rule but `kind="feedback"` (guidance, not hard rule) | same | same |

### Application / never-re-ask

- **Injection:** global/project rules already flow into the agent via
  `_rules_context`; project rules also reach the ticket/doer pipeline via
  `repo_rules` (the `.aiforge/rules/` write). Session rules are injected into
  the live conversation only.
- **Gate enforcement (the "never re-ask" core):** `apply_behavioral` recognizes
  a small set of high-value rule intents and sets per-scope flags the approval
  gates already read:
  - "commit directly / don't ask before committing / machine has access" →
    auto-approve `git commit`/`git add` actions for the scope.
  - "delete without asking / I trust deletes" → set the allow-delete flag for
    the scope (mirrors `AIFORGE_CHAT_ALLOW_DELETE`).
  The gates (`tool_gate`, the chat inline gate, `delete_guard`) check these
  scope-flags before prompting. Other rules are injected as guidance only.

### Transparency

- The `captured` SSE event renders an inline pill in the turn:
  `✓ Saved RULE · global · [change scope ▾] [undo]` (+ any applied flags shown,
  e.g. "commits auto-approved here").
- A "Rules & memories" panel lists captured items grouped by scope, each with
  edit / change-scope / delete.

## Scope of V1 (YAGNI)

- **In:** the classifier pass; Rule/Memory/Feedback × global/project/session
  storage + injection; transparency pill + panel + endpoints; **gate
  enforcement for the commit + delete rule patterns** (covers the operator's
  example end-to-end).
- **Out (follow-up):** mapping ARBITRARY free-text rules to deterministic
  enforcement (hard) — V1 enforces the recognized high-value patterns and
  injects everything else as guidance; contradiction/dedup of accumulated
  rules; a rule-priority/conflict resolver.

## Error handling

- Classifier LLM error / unparseable JSON / low confidence → `category="none"`
  → no capture, normal run proceeds (fail-open: never block a chat turn on
  capture).
- Store failures soft-fail (logged) — a capture failure must not break the
  chat turn.
- A wrong classification is correctable by the user (change-scope / undo), so
  the cost of a misclass is one click, not a wrong permanent rule.

## Testing

- `classify`: strict-JSON parse; fallback to `none` on LLM error / bad JSON /
  low confidence; each category recognized on representative phrasings
  (mocked LLM).
- `store`: routing per category × scope; session isolation (session rule not
  persisted, not visible to another session); project rule writes
  `.aiforge/rules/` AND md_store.
- `apply_behavioral`: the commit-rule + delete-rule patterns set the expected
  scope flags; an arbitrary rule sets none.
- Gate honoring: with a global "commit directly" rule active, the commit
  approval gate does NOT prompt (the operator's example, end-to-end).
- Transparency: `captured` event shape; rescope re-files; undo removes.
- Fail-open: a raising classifier leaves the chat turn unaffected.
