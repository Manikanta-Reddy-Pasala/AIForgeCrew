# Predicting the next step: act when it is safe, ask when it is not

Status: approved 2026-08-26 (three decisions settled with the operator; see
"Decisions" below).

The assistant answers what was asked and stops. Very often the next thing the
user wants is obvious from what they just said — they hand over a host and a
credential, and what they want next is for something to be *connected*; they ask
for a parser to be read, and what they want next is for the bug in it to be
fixed. Today they have to type that second request themselves, every time.

This adds one step at the end of a turn: **work out the likely next action, and
either do it or offer it.**

Three things make it worth building rather than leaving to the model's own
judgement inside a reply:

* it is **bounded** — one capped call, at one point in the turn, with a rule for
  when it may act;
* it is **auditable** — every prediction and its outcome is recorded, so "why
  did it do that" has an answer;
* it **learns** — a prediction the user accepted is evidence about that user's
  work, and feeds the next prediction.

---

## Decisions

Settled with the operator before design, and everything below follows from them.

1. **Act when confident AND the action is safe and reversible. Otherwise ask.**
   Confidence and blast radius are two axes, not one. A confident prediction to
   push to production is still a question.
2. **Persist the prediction outcome, and feed accepted ones back in.** An
   accepted prediction is training data for the next one. This is the learning
   loop, and it is what makes the feature improve rather than plateau.
3. **Simple chat first, pipeline once the behaviour is proven.** The single-agent
   turn is small enough to judge a prediction's quality; a multi-agent turn is
   not, and a lucky prediction there is indistinguishable from a good one.

One thing the operator raised and did not settle, decided here and flagged for
reversal: **credentials handed over mid-chat are held for the session and never
written to disk.** They stay usable for the follow-up step that needs them, are
redacted from the transcript, and are dropped when the chat ends. What *is*
persisted is the shape of the prediction that worked, with values stripped
through `memory.sync.redact` — the filter shipped in the group-sync work, reused
rather than duplicated. This keeps exactly one place in the product where
secrets can be judged.

---

## What already exists

This is mostly composition. Four parts are already in the tree and are used
as-is:

| Part | What it gives us |
|---|---|
| `runtime/rule_capture/` | The exact shape to copy: one capped LLM classify pass, a confidence floor, fail-open on every error, and the "recognise but do not act — offer a revocable opt-in" pattern |
| `runtime/tools/command_risk.assess()` | `SAFE` / `CAUTION` / `DANGEROUS` for a shell command — half of the blast-radius axis, already written |
| `chat_agent/_loop._handle_final` | The one place a turn ends and emits its answer — where a prediction belongs |
| `memory/sync/redact` | Strips credentials out of anything before it is stored |

Nothing here needs a new LLM integration, a new store, or a new approval
mechanism.

---

## The unit: `aiforge_core/runtime/next_step/`

Its own package with a narrow API, mirroring `rule_capture`:

```python
predict(ctx: TurnContext) -> Prediction | None
outcome(prediction_id: str, accepted: bool, *, edited: str = "") -> None
history(limit: int = 20) -> list[dict]
```

```python
@dataclass(frozen=True)
class Prediction:
    id: str            # stable, so the outcome can be recorded against it
    action: str        # one imperative sentence, shown to the user verbatim
    tool: str          # the tool it would call, "" when it is a plain reply
    args: dict         # arguments, already redacted for storage
    confidence: float  # 0.0-1.0, self-reported by the model
    rationale: str     # one clause: why this follows from what just happened
    verdict: str       # ACT | OFFER — decided here, not by the model
```

Four modules, one job each:

* **`_predict.py`** — builds the prompt and makes the one capped call. Inputs:
  the user's message, what the agent actually did this turn (tools called, files
  touched), and the last N *accepted* predictions for this repo. Fails open:
  any error, any unparseable reply, anything below the confidence floor returns
  `None` and the turn ends exactly as it does today.
* **`_risk.py`** — the blast-radius half of the decision. Delegates to
  `command_risk.assess` for shell, and classifies every other tool by what it
  touches. This module owns the ACT/OFFER rule.
* **`_store.py`** — predictions and their outcomes, in the config dir, bounded.
  Every value passes through `redact` before it is written.
* **`__init__.py`** — the API above, and nothing else.

### The ACT / OFFER rule

`_risk.py` answers one question: *if this prediction is wrong, what did it
cost?* Three tiers, and the rule is a table rather than a threshold, because a
threshold is exactly what lets a confident model do something expensive.

| Tier | Examples | Rule |
|---|---|---|
| **Reversible, local** | read a file, search, list, a `SAFE` shell command, draft text | ACT when `confidence >= AIFORGE_PREDICT_MIN_CONFIDENCE` (default 0.75) |
| **Writes the workspace** | edit a file, write a file, `git commit` | ACT only when confident **and** the workspace is a clean git tree — otherwise OFFER, because "undo" has to mean something |
| **Leaves the machine or cannot be undone** | push, deploy, any network write, delete outside the workspace, anything `command_risk` calls `CAUTION`/`DANGEROUS`, anything spending money | **Always OFFER.** No confidence is sufficient. |

An unrecognised tool is treated as the top tier. The unknown case has to be the
careful one, or every new tool is a hole until somebody remembers to classify
it.

### Where it runs

In `_handle_final`, after the answer is emitted and before `done`. That
ordering matters: the user reads the answer either way, and a prediction that
fails or is slow can never delay or replace the thing they actually asked for.

The call is capped (`AIFORGE_PREDICT_TIMEOUT_S`, default 10) and runs on the
`enhancer` role, like `rule_capture` — the agent's own model is not tied up
producing it.

---

## What the user sees

One new event, `{"type": "suggestion", ...}`, emitted between `message` and
`done`. Two shapes:

* **OFFER** — a chip under the reply: *"Next: connect to `db.internal` and
  verify the credentials work"*, with **Do it** and **Dismiss**. Nothing runs
  until it is clicked.
* **ACT** — the same chip, but already done, past tense, with what it did:
  *"Also read `deploy/env.py` — the credential is loaded at line 34."* It says
  what happened; it is not asking.

An ACT is never silent. The whole objection to acting automatically is not
knowing that it happened, and a chip that appears only when there is a question
teaches the user that no chip means nothing was done.

**Dismiss is as informative as Do it**, and both are recorded. A feature that
only learns from its successes drifts.

---

## The learning loop

`_store.py` keeps, per repo:

```json
{"id": "p-8f2c", "at": 1756230000, "repo": "AIForgeCrew",
 "trigger": "user gave a host and a credential",
 "action": "verify the connection with those credentials",
 "tool": "bash", "accepted": true, "edited": ""}
```

The last N accepted rows for the current repo go into the next prediction's
prompt as examples. That is the whole mechanism — no training, no embeddings,
just the model being shown what this user actually said yes to.

Three rules keep it honest:

* **`trigger` and `action` are prose, never argument values.** Everything passes
  `redact.review` before it is written, and a row that fails the filter is
  dropped rather than stored scrubbed. The store must not become the second
  place a credential lives.
* **Bounded** — last 200 rows, and only accepted rows are used as examples,
  though rejected ones are kept for the counters.
* **Per repo.** What the user accepts in one codebase says little about another,
  and mixing them makes every prediction blander.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AIFORGE_PREDICT_DISABLE` | unset | Kill switch. The turn ends exactly as it does today. |
| `AIFORGE_PREDICT_MIN_CONFIDENCE` | `0.75` | Below this, no prediction at all — not even an offer. |
| `AIFORGE_PREDICT_ACT` | `1` | `0` makes every prediction an OFFER. The "always ask me" setting. |
| `AIFORGE_PREDICT_TIMEOUT_S` | `10` | Cap on the one call. |
| `AIFORGE_PREDICT_ROLE` | `enhancer` | Which model role predicts. |

All surfaced in Settings beside the memory-sync panel, since both answer "what
is this thing doing on its own".

---

## Failure behaviour

Everything fails open, and the reason is the same as `rule_capture`'s: this runs
on every turn, and a feature that improves a good turn must never be able to
break one.

* The model is unreachable, slow, or returns nonsense → no suggestion, turn ends
  normally.
* The store is unreadable → predict without examples.
* `redact` cannot judge a row → the row is not stored.
* An ACT's tool call fails → the chip says so; the turn is already complete and
  is not retried or rolled back.

---

## Testing

* **The ACT/OFFER table**, one case per tier, both directions — a `SAFE` read
  acts, a `git push` never acts however confident, an unknown tool never acts.
* **Confidence floor** — below it, nothing is emitted at all.
* **Fail-open** — a raising predictor, a timing-out one and one returning
  non-JSON each leave the turn's `message`/`done` events byte-identical to
  today's.
* **Ordering** — `suggestion` always arrives after `message`, never before.
* **The learning loop** — an accepted prediction appears in the next prompt; a
  dismissed one does not; the store stays bounded.
* **No secrets stored** — a prediction whose trigger contains a credential is
  dropped, and the store never contains the value.
* **`AIFORGE_PREDICT_ACT=0`** turns every ACT into an OFFER.
* **Disabled** — with the kill switch set, the event never appears and no LLM
  call is made.

## Definition of done

Simple chat only. Full suite green against the `main` baseline, SonarQube clean
of new findings, merged. Pipeline support is a separate piece of work once the
prediction quality is known — and the store's counters are what will tell us
whether it is good enough to be worth extending.
