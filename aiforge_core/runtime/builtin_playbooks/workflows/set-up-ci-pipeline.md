---
name: set-up-ci-pipeline
description: Procedure to add a CI pipeline that gates merges on quality
triggers: [ci, continuous integration, pipeline, github actions, gitlab ci, build pipeline]
source: builtin
---

1. **Define the gate**: what must pass to merge — install, build, full test suite, lint, format check, typecheck, security/dep audit.
2. **Write the config** for the project's CI (Actions/GitLab CI/etc): trigger on PR + default branch; cache deps for speed.
3. **Reproduce the local verify loop in CI** exactly, so green-local = green-CI. Pin tool versions.
4. **Fail the build on any gate failure**; make logs clear about what broke.
5. **Make it fast** (cache, parallelize jobs) — a slow pipeline gets bypassed.
6. **Require the check** on the protected branch so red can't merge.
7. (Optional next) add deploy/release stages, coverage reporting, and artifact publishing once the gate is solid.
