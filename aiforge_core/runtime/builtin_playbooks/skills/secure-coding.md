---
name: secure-coding
description: Avoid the common security holes (OWASP-style) while writing code
triggers: [security, owasp, injection, xss, auth, secret, vulnerability, sanitize]
source: builtin
---

- **Never trust input.** Validate type/range/format at the boundary; reject by allowlist, not blocklist.
- **Parameterize queries** (no string-built SQL). Escape/encode output for the sink (HTML, shell, JSON, URL).
- **No secrets in code/logs/commits.** Read from env/secret store; rotate if leaked.
- **AuthN ≠ AuthZ.** Check the caller is allowed to act on THIS object, server-side, every request.
- **Least privilege** for tokens, DB users, file perms, container caps.
- **Crypto:** use vetted libraries + current algorithms; never roll your own. Hash passwords with bcrypt/argon2, not MD5/SHA1.
- **Dependencies:** pin + scan; a known-vuln transitive dep is your vuln.
- **Fail closed**, don't leak stack traces / internals in error responses.
