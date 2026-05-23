# Deploy recipe — TallyConnector

TallyConnector ships as a Windows-side console app that connects to
Tally over COM. There is no QA cluster auto-deploy — operators copy
the new artifact to the Windows box and restart the service. The
AIForge runner has no Windows access, so this recipe is mostly
**hand-off instructions** for the operator's Windows-side Claude.

## Step 0 — gate

```bash
if [ "${AIFORGE_AUTO_MERGE:-0}" != "1" ]; then
  echo "deploy: skipped (AIFORGE_AUTO_MERGE=0)"
  echo "deploy: PR $PR_URL awaits manual merge; Windows redeploy needed"
  exit 0
fi
```

## Step 1 — merge

```bash
gh pr merge "$PR_URL" --squash --delete-branch --auto || exit 1
```

## Step 2 — wait for artifact build (max 8 min)

```bash
for i in $(seq 1 48); do
  CONCLUSION=$(gh run list --repo OneShellSolutions/TallyConnector \
    --branch master --limit 1 --json conclusion -q '.[0].conclusion')
  [ "$CONCLUSION" = "success" ] && break
  [ "$CONCLUSION" = "failure" ] && { echo "deploy: ci failed"; exit 1; }
  sleep 10
done
```

## Step 3 — emit handoff brief

Don't try to deploy to Windows from here. Emit a structured
instruction the operator (or their Windows-side Claude) can act on:

```json
{
  "windows_redeploy": {
    "artifact_url": "<latest .exe / .msi from the GHA artifacts>",
    "target_machine": "operator-laptop",
    "steps": [
      "Download artifact",
      "Stop OneshellTallySync service in Windows Services",
      "Copy new binary into C:\\Program Files\\OneShell\\TallyConnector\\",
      "Start service",
      "Open Tally → run a manual sync → confirm fix"
    ]
  }
}
```

The verify recipe will set ``live_handoff=true`` in its verdict so
this brief lands as a PR comment.
