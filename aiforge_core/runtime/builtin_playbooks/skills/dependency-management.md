---
name: dependency-management
description: Add, pin, and upgrade dependencies without breaking the build
triggers: [dependency, package, upgrade, version, lockfile, npm, pip, vulnerability]
source: builtin
---

- **Pin + lock.** Commit the lockfile; reproducible installs across machines/CI.
- **Add deliberately**: prefer the stdlib or an existing dep over a new one. Check the package's maintenance, license, size, transitive weight.
- **Upgrade in small steps**: read the changelog/breaking notes; bump one (or one group) at a time; run tests + lint after each.
- **Security**: run the audit tool; treat known-vuln deps as bugs to fix, including transitive ones.
- **Don't vendor a giant lib for one function** you can write in a few lines.
- After any change: full install from a clean state + the test suite must pass.
