---
name: tests-by-type
description: Pick the right test type for the change and write it from a reusable template (unit, integration, API/contract, e2e, load, security, regression)
triggers: [what test, which test, test type, add tests, write a test, unit test, integration test, api test, e2e test, load test, security test, regression test, coverage]
source: builtin
---

Match the test to what you changed. Pick the type, then fill the template. Keep tests deterministic, isolated, and asserting on VALUES not incidental strings.

**Unit** — one function/class, no I/O. Table-driven over the interesting cases + edges (empty, zero, boundary, error).
```python
import pytest
@pytest.mark.parametrize("inp,expected", [(2,4),(0,0),(-3,9)])
def test_square(inp, expected):
    assert square(inp) == expected
def test_square_rejects_none():
    with pytest.raises(TypeError):
        square(None)
```

**Integration** — a unit + a real dependency (DB, cache, queue). Set up/tear down real state; assert the SIDE EFFECT, not just the return.
```python
def test_repo_saves_and_reads(db):          # db = fixture giving a clean schema
    repo = OrderRepo(db)
    repo.save(Order(id=1, total=10))
    assert repo.get(1).total == 10
    assert db.execute("select count(*) from orders").scalar() == 1
```

**API / contract** — hit the endpoint (TestClient or live URL). Assert status + parse the body + check the schema/fields.
```python
def test_create_user(client):
    r = client.post("/users", json={"name": "a"})
    assert r.status_code == 201
    body = r.json(); assert body["id"] and body["name"] == "a"
    assert client.get(f"/users/{body['id']}").status_code == 200   # round-trip
```
Cover the error contract too: 400 bad input, 401/403 unauth, 404 missing, 409 conflict.

**E2E** — the whole user journey across components (browser/CLI/multi-service). One happy path + one critical error path; verify final observable state.

**Load / performance** — concurrency + latency budget, not correctness.
```python
def test_p95_under_budget(bench):
    lat = bench(concurrency=50, n=1000, call=lambda: client.get("/search?q=x"))
    assert lat.p95_ms < 200 and lat.error_rate == 0
```

**Security** — the abuse cases: authz (a user can't read another's data), input validation / injection, secrets not leaked, rate-limit enforced.
```python
def test_cannot_read_others_order(client, alice, bob):
    oid = create_order(as_=alice)
    assert client.get(f"/orders/{oid}", auth=bob).status_code in (403, 404)
```

**Regression** — for a fixed bug: a test that FAILS on the old code and passes after the fix, named for the bug/issue. Locks it so it can't silently return.

Rule of thumb: change a pure function → unit; touch a boundary (DB/HTTP/queue) → integration + API; ship a feature → add e2e for the main flow; fix a bug → regression. Test behavior, never re-assert the implementation.
