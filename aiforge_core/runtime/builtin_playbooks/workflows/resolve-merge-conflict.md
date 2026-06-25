---
name: resolve-merge-conflict
description: Procedure to resolve a merge/rebase conflict correctly
triggers: [merge conflict, resolve conflict, rebase conflict, git conflict]
source: builtin
---

1. **Know what you're integrating**: `git log --oneline` both sides; understand WHY each side changed the conflicting lines.
2. **Start clean**: ensure the working tree is committed/stashed before the merge/rebase.
3. For each conflict: open the file, read BOTH versions. Keep the INTENT of each change — don't blindly pick one side; often you need both edits combined.
4. Remove all conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`). Search the repo to confirm none remain.
5. **Run the build + tests** — a syntactically-merged file can still be semantically broken.
6. `git add` resolved files, then `git rebase --continue` / commit the merge. If it's a tangle, `--abort` and redo deliberately.
