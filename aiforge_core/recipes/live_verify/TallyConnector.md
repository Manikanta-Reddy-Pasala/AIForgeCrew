# Live verify — TallyConnector

You are the **live_verifier** for TallyConnector. Tally itself requires
Windows + IIS + COM bindings that don't run on the Linux NUC, so this
recipe has **two paths**: a deterministic fixture path that always
runs, and an optional live handoff to a Windows-side Claude.

## Fixture path (mandatory)

Identify the new / modified test class(es):

```bash
git diff --name-only origin/master...HEAD -- 'src/test/**' | \
  grep -E '\.java$'
```

Run each one:

```bash
./mvnw -q -Dtest='<TestClassName>' test 2>&1 | tail -40
```

Pass criteria: exit 0 AND `Tests run: N, Failures: 0, Errors: 0`.

If the Doer only added tests AND no `src/main` files changed,
**fail fast** — `git_pr` will already reject the PR, but emit a
clear verdict so the operator sees why:

```json
{"ok": false, "rationale": "test-only diff", "fixture_count": 1}
```

## Live handoff path (optional)

The fix may need verification against a real Tally instance. The
operator's Windows machine runs Claude with Tally COM access. We
signal "please verify live" via the verdict:

```json
{
  "ok": true,
  "rationale": "Fixture tests green; recommend live verification",
  "fixture_count": 2,
  "live_handoff": true,
  "handoff_brief": "Open Spandana company in Tally, create credit note 12 with 8 line items at 18% IGST, run the sync, confirm all 8 lines show tax in Oneshell UI"
}
```

`live_handoff=true` does NOT block the PR — it adds a comment so a
human or the operator's Windows Claude knows to verify before merge.
`live_handoff=false` means fixtures alone are sufficient.

## Verdict shape

Always emit:

```json
{
  "ok": bool,
  "rationale": "1-sentence summary",
  "fixture_count": int,
  "live_handoff": bool,
  "handoff_brief": "string-or-empty"
}
```
