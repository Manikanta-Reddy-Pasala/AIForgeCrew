---
name: aiforge-fetch
description: Allowlisted outbound HTTP (GET/HEAD). Only for roles with `network_fetch: true` (Tester, Sr Dev). Domain allowlist lives at security/network-allowlist.yml — arbitrary URLs rejected.
version: 1.0.0
platforms: [macos]
---

# aiforge-fetch

## Fetch

```bash
{{AIFORGE_PY}} - <<'PY'
from pathlib import Path
from aiforge_core.net import fetch_url
r = fetch_url(Path("."), {"url": "https://api.github.com/repos/owner/repo/issues/42", "method": "GET"})
print(r["status"], r["body"][:500])
PY
```

## Allowed domains (default)

- `github.com`, `raw.githubusercontent.com`, `api.github.com`
- `huggingface.co`, `pypi.org`
- `docs.python.org`, `peps.python.org`, `*.readthedocs.io`
- `stackoverflow.com`, `developer.mozilla.org`, `*.mozilla.org`
- `lmstudio.ai`

Any other host → `FetchDenied`. Edit `security/network-allowlist.yml` to add, commit that change for audit.

## Limits

- Method: `GET` or `HEAD` only (no mutation verbs)
- Body cap: 500 KB (truncates, marks `truncated: true`)
- Timeout: 15 s
- Private / loopback IPs denied unless explicitly allowed in the YAML

## When to use

- Tester: look up framework docs, confirm API shapes before writing tests.
- Sr Dev: check stdlib edge cases, reproduce upstream bug reports.
- EM / Sr Arch: NOT allowed — their roles have `network_fetch: false`.
