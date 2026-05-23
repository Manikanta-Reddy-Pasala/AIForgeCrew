# Deploy recipe — generic

How the candidate fix reaches the environment the live_verifier will
exercise. Run BEFORE the verify recipe's "Exercise the change" step.

Honour the operator's autonomy gate:

```bash
[ "${AIFORGE_AUTO_MERGE:-0}" = "1" ] || { echo "auto-merge off — skip"; exit 0; }
```

When the gate is OFF (default): emit a note in your evidence trail
that the fix exists in a PR but is not yet on the verify target, and
let the verify step run against the EXISTING deployed version (which
will likely still show the bug — that's the honest answer, not a
failure of the recipe).

When ON, the generic flow is:

1. Merge the PR opened by ``git_pr``:
   ```bash
   gh pr merge "$PR_URL" --squash --delete-branch
   ```
2. Wait for the project's CI to publish a new image / artifact.
3. Wait for the deploy mechanism (Argo / Helm / Tekton / Fly / Vercel)
   to roll the new artifact out.
4. Confirm the deployed instance now reports the new commit SHA via
   ``/actuator/info``, ``/version``, or equivalent.

If your project has no CI/CD, override this recipe with a project-
specific one under ``aiforge_core/recipes/deploy/<project>.md``.
