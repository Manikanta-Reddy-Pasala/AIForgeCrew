# Tester

**Role**: emit failing TDD test specs targeting the planned changes.

## What it does

Single LLM call (json_object) returning
`{tests:[{name, target_class, target_method, scenario, expected, framework}], coverage_target}`.

Frameworks supported: `junit5 | pytest | jest | mockito`.

The test list is descriptive (specs, not actual code) — it tells
downstream tools what to assert. Test code generation is a Doer step
(`action=create`, target = `src/test/java/.../FooTest.java`).

## Inputs

- `understanding` (with `context_md` stripped)
- `plan`
- `failures_hint`

## Outputs

- `test_plan.tests[]`
- `test_plan.coverage_target` (default 0.8)

## Auto-learn

- Receives `failures_hint` with the header
  *"Mistakes from prior tickets — write tests covering these"* so
  the Tester is biased toward asserting on previously-broken
  behaviours.
- Test specs become part of the ticket metadata; the Architect can
  see them when reviewing.

## Config

| key | default |
|---|---|
| `model` | `Qwen3-Coder-Next-MLX-4bit` |
| `temperature` | 0.1 |
| `max_tokens` | 12000 |
