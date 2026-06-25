---
name: write-release-notes
description: Procedure to produce a changelog / release notes from the changes
triggers: [release notes, changelog, release, what's new, version notes]
source: builtin
---

1. **Gather the changes** since the last release: merged PRs / commits (`git log <last-tag>..HEAD`), grouped by ticket.
2. **Categorize**: Features · Improvements · Bug fixes · Breaking changes · Security · Deprecations.
3. **Write for the READER, not the committer**: each line says what changed and why it matters to a user — not the internal refactor detail.
4. **Surface breaking changes loudly** with the migration step required.
5. **Credit + link**: reference issue/PR numbers; thank contributors if public.
6. **Version + date** per the project's scheme (semver). Keep a running CHANGELOG.md, newest on top.
7. Keep it honest and skimmable; lead with the high-impact items.
