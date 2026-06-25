---
name: api-design
description: Design clean, consistent HTTP/REST APIs
triggers: [api, rest, endpoint, http, openapi, route, resource]
source: builtin
---

- **Resources as nouns**, plural: `/orders`, `/orders/{id}/items`. Verbs are the HTTP methods.
- **Methods**: GET (safe, idempotent), POST (create), PUT/PATCH (update), DELETE. Idempotency where it matters.
- **Status codes** honestly: 200/201/204, 400 (bad input), 401/403 (authn/authz), 404, 409 (conflict), 422 (validation), 429, 5xx.
- **Consistent shapes**: same error envelope everywhere; ISO-8601 timestamps; explicit field names.
- **Validate input** + return actionable error messages (which field, why).
- **Pagination, filtering, sorting** as query params; don't return unbounded lists.
- **Version** breaking changes (`/v2` or header). Don't break existing clients silently.
- Document it (OpenAPI) and keep the doc in sync with the code.
