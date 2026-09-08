"""Keep a CA test's environment inside that test.

``ca.save()`` / ``ca.add()`` deliberately call ``apply_to_process_env(force=True)``
so a bundle pasted in the UI takes hold with no restart — it writes
``SSL_CERT_FILE`` and friends straight into ``os.environ``. In a test that is a
LEAK: ``monkeypatch.delenv(var, raising=False)`` on a variable that was absent
records nothing to undo, so a value the test body set outlives it and every
later module runs with a CA bundle configured. That is what made
``test_auto_relax_internal`` and ``test_insecure_context_verifies_against_the_pin``
pass alone and fail in a full run — the whole suite's answer to "is a CA
configured?" depended on file order.
"""
from __future__ import annotations

import os

import pytest

from aiforge_core.net import ca

_VARS = (set(ca.ENV_VARS) | set(ca.SUBPROCESS_VARS)
         | {"AIFORGE_LLM_CA_BUNDLE", "AIFORGE_CONFIG_DIR"})


@pytest.fixture(autouse=True)
def _ca_env_stays_local():
    before = {name: os.environ.get(name) for name in _VARS}
    yield
    for name, value in before.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
