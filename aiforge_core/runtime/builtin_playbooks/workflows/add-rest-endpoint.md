---
name: add-rest-endpoint
description: Procedure to add a new HTTP/REST endpoint end to end
triggers: [add endpoint, new api, rest endpoint, new route, add api]
source: builtin
---

1. **Design** (see `api-design` skill): method + path + request/response shape + status codes + authz rule.
2. **Model/validation**: define the request DTO with validation; reject bad input with a clear 400/422.
3. **Route → handler → service → data**: wire the layers following the project's existing pattern; keep business logic out of the controller.
4. **AuthZ**: enforce who may call it, server-side.
5. **Tests first/alongside**: success, validation failure, auth failure, and a key edge case.
6. **Docs**: update the OpenAPI/spec; example request/response.
7. **Verify**: tests + lint/typecheck green; hit the endpoint locally (curl) to confirm real behavior; check it doesn't break existing routes.
