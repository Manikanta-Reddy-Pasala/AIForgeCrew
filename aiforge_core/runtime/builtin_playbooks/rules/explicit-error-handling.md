---
name: explicit-error-handling
alwaysApply: true
---
# Handle errors explicitly
- Never swallow exceptions silently; log or surface them with context.
- Validate inputs and fail fast with a clear message.
- Don't catch broad exceptions unless you re-raise or handle each case.
