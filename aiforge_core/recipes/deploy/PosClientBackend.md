# Deploy recipe — PosClientBackend

Pipeline that ships a fix to ``pos-api.oneshell.in``:

```
PR open  →  merge to master  →  Tekton image build  →
ArgoCD sync ``qa-apps/posclientbackend``  →  k8s rolling update
→  new pod Ready  →  /actuator/info exposes new commit SHA
```

You will be invoked with two relevant env vars:

* ``PR_URL`` — GitHub PR URL the ``git_pr`` stage just opened.
* ``AIFORGE_AUTO_MERGE`` — ``1`` to merge + wait autonomously,
  anything else to skip.

## Step 0 — gate

```bash
if [ "${AIFORGE_AUTO_MERGE:-0}" != "1" ]; then
  echo "deploy: skipped (AIFORGE_AUTO_MERGE=0)"
  echo "deploy: PR $PR_URL awaits manual merge; verify will hit current QA"
  exit 0
fi
```

## Step 1 — capture pre-merge SHA

```bash
PREV_SHA=$(curl -sf https://pos-api.oneshell.in/actuator/info | \
  python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("git",{}).get("commit",{}).get("id","none"))')
echo "deploy: pre-merge sha=$PREV_SHA"
```

## Step 2 — merge

```bash
gh pr merge "$PR_URL" --squash --delete-branch --auto || \
  { echo "deploy: gh pr merge failed"; exit 1; }
```

``--auto`` queues the merge if branch protection requires checks; the
merge fires as soon as required checks pass.

## Step 3 — wait for Tekton build (max 10 min)

```bash
# PipelineRun is named like ``posclientbackend-<sha>-<n>``. Watch
# the latest one tied to this branch.
for i in $(seq 1 60); do
  STATUS=$(kubectl --insecure-skip-tls-verify get pipelinerun -n tekton-pipelines \
    -l app=posclientbackend --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1].status.conditions[0].reason}' 2>/dev/null)
  case "$STATUS" in
    Succeeded) echo "deploy: tekton OK"; break ;;
    Failed|TaskRunFailed|Cancelled)
      echo "deploy: tekton $STATUS"; exit 1 ;;
  esac
  sleep 10
done
[ "$STATUS" = "Succeeded" ] || { echo "deploy: tekton timeout"; exit 1; }
```

If ``kubectl`` is unavailable on the runner: tail GitHub Actions
instead via ``gh run watch``.

## Step 4 — wait for ArgoCD sync (max 5 min)

```bash
for i in $(seq 1 30); do
  SYNC=$(kubectl --insecure-skip-tls-verify get app posclientbackend-qa -n argocd \
    -o jsonpath='{.status.sync.status}' 2>/dev/null)
  HEALTH=$(kubectl --insecure-skip-tls-verify get app posclientbackend-qa -n argocd \
    -o jsonpath='{.status.health.status}' 2>/dev/null)
  [ "$SYNC" = "Synced" ] && [ "$HEALTH" = "Healthy" ] && break
  sleep 10
done
[ "$SYNC" = "Synced" ] || { echo "deploy: argocd sync=$SYNC"; exit 1; }
```

## Step 5 — confirm new SHA is live (max 3 min)

```bash
for i in $(seq 1 18); do
  NEW_SHA=$(curl -sf https://pos-api.oneshell.in/actuator/info | \
    python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("git",{}).get("commit",{}).get("id","none"))')
  if [ "$NEW_SHA" != "$PREV_SHA" ] && [ "$NEW_SHA" != "none" ]; then
    echo "deploy: live sha=$NEW_SHA (was $PREV_SHA)"; exit 0
  fi
  sleep 10
done
echo "deploy: SHA still $PREV_SHA after 3min"; exit 1
```

## Failure semantics

Any non-zero exit from a step above means the deploy didn't reach
QA. The verify recipe still runs but should emit ``ok=false`` with
rationale ``"fix not on QA"``. Do NOT pretend the verify passed
because the test against stale code happens to pass.
