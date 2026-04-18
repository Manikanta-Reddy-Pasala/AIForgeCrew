---
name: aiforge-git
description: Role-scoped git operations. Tester + Sr Dev can branch + commit (path-scoped by role write ACL). Sr Architect creates MRs via gh CLI. Every commit validates paths against security/file-access-rules.yml before `git add`.
version: 1.0.0
platforms: [macos]
---

# aiforge-git

## Branch (tester + sr-developer only)

```bash
{{AIFORGE_PY}} - <<'PY'
from pathlib import Path
from aiforge_core.git_ops import GitOps
print(GitOps(Path(".")).branch("tester", "feat/TICKET-abc"))
PY
```

## Commit (path-scoped by role)

- Tester writes `tests/**` only. Commits containing `src/**` rejected.
- Sr Dev writes `src/**` only. Commits containing `tests/**` rejected.

```bash
{{AIFORGE_PY}} - <<'PY'
from pathlib import Path
from aiforge_core.git_ops import GitOps
print(GitOps(Path(".")).commit(
    role="sr-developer",
    paths=["src/auth/jwt.py", "src/auth/__init__.py"],
    message="fix(auth): use <= for token expiry edge",
))
PY
```

## Create MR (sr-architect only)

```bash
{{AIFORGE_PY}} - <<'PY'
from pathlib import Path
from aiforge_core.git_ops import GitOps
print(GitOps(Path(".")).create_mr(
    role="sr-architect",
    title="TICKET-abc: fix JWT expiry edge",
    body="See ticket for full context.",
    source_branch="feat/TICKET-abc",
))
PY
```

Requires `gh` CLI. Without it, returns `{ok: false, reason: "gh_cli_missing"}` so the architect can escalate to human.
