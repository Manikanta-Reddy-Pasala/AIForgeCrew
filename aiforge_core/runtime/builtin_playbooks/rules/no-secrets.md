---
name: no-secrets
alwaysApply: true
---
# No secrets in code
- Never hardcode API keys, tokens, passwords, or connection strings.
- Read secrets from environment variables or a secret store.
- Never commit `.env` files or credentials; add them to `.gitignore`.
