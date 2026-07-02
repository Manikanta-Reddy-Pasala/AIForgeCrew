"""graph_neighbours must always close its Neo4j driver — the original code put
``drv.close()`` INSIDE the try after the session block, so a raising
``session().run()`` jumped to except and leaked the driver (every other driver
site uses ``finally: drv.close()``)."""
import sys
import types

import pytest


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, *a, **k):
        raise RuntimeError("boom from neo4j run()")


class _FakeDriver:
    def __init__(self):
        self.closed = False

    def session(self):
        return _FakeSession()

    def close(self):
        self.closed = True


@pytest.fixture
def code_context(monkeypatch):
    from aiforge_core.memory import code_context as cc

    drivers = []

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri, auth=None):
            d = _FakeDriver()
            drivers.append(d)
            return d

    fake_neo4j = types.ModuleType("neo4j")
    fake_neo4j.GraphDatabase = _FakeGraphDatabase
    monkeypatch.setitem(sys.modules, "neo4j", fake_neo4j)

    from aiforge_core.memory import neo4j_conn
    monkeypatch.setattr(neo4j_conn, "neo4j_params",
                        lambda: ("bolt://x", "neo4j", "pw"))
    monkeypatch.setenv("AIFORGE_DOER_GRAPH_NEIGHBOURS", "1")
    return cc, drivers


def test_driver_closed_even_when_run_raises(code_context):
    cc, drivers = code_context
    out = cc.graph_neighbours(["src/main/java/A.java"])
    assert out == ""                       # graceful empty on error
    assert drivers, "expected a driver to have been created"
    assert drivers[0].closed is True       # ... and always closed (no leak)
