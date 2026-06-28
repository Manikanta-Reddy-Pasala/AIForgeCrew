"""L1 scaffold sanity — module tree imports cleanly."""
from __future__ import annotations


def test_codemem_imports() -> None:
    pass


def test_codemem_version_marker() -> None:
    from aiforge_memory import SCHEMA_VERSION
    assert SCHEMA_VERSION == "codemem-v1"
