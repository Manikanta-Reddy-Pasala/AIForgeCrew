"""L1 scaffold sanity — module tree imports cleanly."""
from __future__ import annotations


def test_codemem_imports() -> None:
    import aiforge_core.codemem
    import aiforge_core.codemem.ingest
    import aiforge_core.codemem.store
    import aiforge_core.codemem.api


def test_codemem_version_marker() -> None:
    from aiforge_core.codemem import SCHEMA_VERSION
    assert SCHEMA_VERSION == "codemem-v1"
