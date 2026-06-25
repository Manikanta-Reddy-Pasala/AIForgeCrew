---
name: rotate-a-secret
description: Procedure to rotate a credential/key with zero downtime
triggers: [rotate secret, rotate key, credential rotation, api key, password rotation, leaked]
source: builtin
---

Dual-key overlap, never a hard cutover.

1. **Generate the NEW secret** alongside the old (most systems allow two valid creds during overlap).
2. **Distribute** the new secret to the consuming services (secret store / env), without removing the old yet.
3. **Roll services** to pick up the new value; confirm they authenticate successfully on it.
4. **Verify** nothing is still using the old one (logs/metrics for auth with the old key).
5. **Revoke the old** secret only after all consumers use the new.
6. **If LEAKED**: revoke immediately (accept brief disruption), rotate, then audit access + scrub it from git history/logs.
7. Never commit secrets; store in a vault/secret manager; short TTLs where possible.
