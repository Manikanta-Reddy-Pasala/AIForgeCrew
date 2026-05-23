# Live verify — generic recipe

You are the **live_verifier**. The Doer already wrote a candidate fix
in the current worktree and the Validator approved the diff. Your job
is to confirm the fix actually works by running the project's own test
or build command end-to-end.

## Procedure

1. Detect build system from files in cwd:
   - `pom.xml` or `mvnw` → Java/Maven → `./mvnw test -q`
   - `package.json` → Node → `npm test --silent`
   - `pyproject.toml` or `setup.py` → Python → `uv run pytest -x -q || python -m pytest -x -q`
   - `Makefile` with a `test` target → `make test`
   - Otherwise → emit `{"ok": false, "rationale": "unknown build system"}`

2. Run the command. Capture exit code + last 60 lines of output.

3. Emit JSON to `state['live_verifier_verdict']`:

```json
{
  "ok": true,
  "command": "./mvnw test -q",
  "exit_code": 0,
  "rationale": "Build green, 47 tests passed.",
  "evidence": ["last lines of stdout..."]
}
```

`ok=true` only when exit code is 0 AND output does NOT mention
`FAILED`, `Tests run.*Failures: [1-9]`, `BUILD FAILURE`, or
`AssertionError`.
