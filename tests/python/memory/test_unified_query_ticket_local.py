"""_ticket_local wrapped BOTH the psycopg.connect AND the f-string result
shaping in a broad ``except: return None`` — a renamed column / key typo was
indistinguishable from a DB outage (silently None, nothing recorded). The DB
part must catch only (psycopg.Error, OSError); a shaping KeyError must surface."""
import pytest

psycopg = pytest.importorskip("psycopg")


class _FakeCursor:
    def __init__(self, ticket):
        self._ticket = ticket

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._ticket

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, ticket):
        self._ticket = ticket

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self._ticket)


@pytest.fixture
def uq():
    from aiforge_core.memory import unified_query as u
    return u


def test_db_error_returns_none(uq, monkeypatch):
    def _boom(*a, **k):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", _boom)
    assert uq._ticket_local("ONE-100") is None   # graceful, not a crash


def test_keyerror_in_shaping_surfaces(uq, monkeypatch):
    # A renamed column: the ticket row is missing 'title' → shaping KeyError.
    bad_row = {"id": 1, "identifier": "ONE-100", "status": "todo",
               "body": "b", "created": "2026-07-01 10:00",
               "updated": "2026-07-01 10:05"}  # NO 'title'
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _FakeConn(bad_row))

    with pytest.raises(KeyError):
        uq._ticket_local("ONE-100")   # must NOT be swallowed to None


def test_happy_path_shapes_text(uq, monkeypatch):
    row = {"id": 1, "identifier": "ONE-100", "status": "todo", "title": "hi",
           "body": "b", "created": "2026-07-01 10:00",
           "updated": "2026-07-01 10:05"}
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _FakeConn(row))
    out = uq._ticket_local("ONE-100")
    assert out and "ONE-100" in out["text"] and "hi" in out["text"]
