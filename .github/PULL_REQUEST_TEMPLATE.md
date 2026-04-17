## Ticket

Fixes #<ticket-id>

## Summary

<one paragraph — what and why>

## TDD trail
- [ ] Tests were written BEFORE production code (Tester phase first)
- [ ] All new/changed behavior has at least one unit test
- [ ] No test files were modified by Sr Dev after Tester committed them
- [ ] Coverage ≥ 80% on changed lines (report attached in ticket)

## Security
- [ ] No secrets committed
- [ ] No writes to `.env*`, `secrets/`, `config/prod/`, `.github/workflows/**`
- [ ] Sr Architect reviewed security implications

## Review
- [ ] Sr Architect approved on ticket
- [ ] Ticket contains full review trail (plan → tests → code → review → MR)

## Automation
- [ ] `make validate` passes
- [ ] `make lint` passes
- [ ] `make test` passes
