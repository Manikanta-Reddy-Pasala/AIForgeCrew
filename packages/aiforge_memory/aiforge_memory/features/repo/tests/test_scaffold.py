"""L1 scaffold sanity — module tree imports cleanly."""
from __future__ import annotations


def test_codemem_imports() -> None:
    """The imports ARE the assertion: a broken module tree raises here.

    The body used to be `pass`, so the test's name was the only thing claiming
    anything — it passed with the package uninstallable.
    """
    import aiforge_memory
    from aiforge_memory.features.chunk import embed
    from aiforge_memory.features.repo import extract
    from aiforge_memory.features.symbol import extract_calls

    assert aiforge_memory.SCHEMA_VERSION
    assert embed and extract and extract_calls


def test_codemem_version_marker() -> None:
    from aiforge_memory import SCHEMA_VERSION
    assert SCHEMA_VERSION == "codemem-v1"
