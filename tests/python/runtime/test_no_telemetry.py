"""Network+telemetry lockdown — LiteLLM phone-home must be OFF.

LiteLLM's ``telemetry`` attr defaults True → it POSTs anonymous usage to
PostHog on import/first-call. :func:`_quiet_litellm` (run at import of
``escalating_llm``) must flip it False so there is NO unsolicited egress.
"""
from __future__ import annotations

import sys
import types

from aiforge_core.runtime import escalating_llm


def test_quiet_litellm_sets_telemetry_false(monkeypatch):
    """A fake ``litellm`` module: after _quiet_litellm(), telemetry is False."""
    fake = types.ModuleType("litellm")
    fake.telemetry = True
    fake.suppress_debug_info = False
    fake.set_verbose = True
    fake.success_callback = ["x"]
    fake.failure_callback = ["y"]
    fake._async_success_callback = ["z"]
    fake._async_failure_callback = ["w"]
    monkeypatch.setitem(sys.modules, "litellm", fake)

    escalating_llm._quiet_litellm()

    assert fake.telemetry is False
    # sanity: the existing callback-nulling still runs
    assert fake.success_callback == []
    assert fake.failure_callback == []


def test_real_litellm_telemetry_off_after_boot():
    """If litellm is importable, calling _quiet_litellm() flips the real attr."""
    try:
        import litellm  # noqa: F401
    except Exception:  # noqa: BLE001
        import pytest

        pytest.skip("litellm not installed")
    escalating_llm._quiet_litellm()
    import litellm as _l

    assert _l.telemetry is False


def test_quiet_litellm_runs_at_import():
    """The module invokes _quiet_litellm() at import (line ~224)."""
    src = escalating_llm.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    # top-level (unindented) call, applied before any team run
    assert "\n_quiet_litellm()" in text
