---
name: git-operations
description: Use git safely — branch, commit, rebase, resolve conflicts
triggers: [git, branch, commit, rebase, merge, conflict, stash, cherry-pick]
source: builtin
---

Work git deliberately; never force-push shared branches.

- **Branch per task.** `git switch -c <type>/<desc>` off an up-to-date default. Never commit to `main`/`master` directly.
- **Small, atomic commits.** One logical change each; message says WHAT + WHY (imperative subject ≤ 72 chars, body for the why).
- **Before committing**, `git status` + `git diff --staged` — stage intentionally, not `git add -A` blindly. Check for stray files, debug prints, secrets.
- **Rebase to tidy local history** (`git rebase -i`), but NEVER rebase/force-push a branch others use.
- **Conflicts:** read both sides, keep the intended behavior of each, run tests after resolving. `git rebase --abort` if it gets messy — restart deliberately.
- **Undo safely:** `git revert` for shared history; `git reset --soft` only on local commits. `git reflog` recovers "lost" commits.
