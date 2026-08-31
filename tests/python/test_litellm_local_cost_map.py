"""litellm must not reach out to raw.githubusercontent.com on import.

It fetches its model cost/context map from GitHub every time it is imported
and warns when that fails ("Failed to fetch remote model cost map ... Falling
back to local backup"). This is a local-model deployment, so the call cannot
produce a better answer than the backup map bundled in the wheel — which is
exactly what the failure falls back to. ``aiforge_core/__init__`` sets
LITELLM_LOCAL_MODEL_COST_MAP before anything can import litellm.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def test_importing_aiforge_core_sets_the_local_cost_map_default():
    """setdefault, so an operator can still opt back into the remote map."""
    import aiforge_core  # noqa: F401
    assert os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") == "True"


def test_operator_can_still_opt_into_the_remote_map(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "False")
    import importlib

    import aiforge_core
    importlib.reload(aiforge_core)
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "False", \
        "the default must never overwrite an explicit choice"


# NOTE ON THE SHAPE OF THIS CHECK: raising from the patched httpx.get does NOT
# work as a probe. get_model_cost_map wraps the fetch in `except Exception` and
# falls back to the backup map, so the exception is swallowed and the test
# passes whether or not the guard is in place — it was written that way first
# and proved nothing. RECORD the attempt instead and assert on the record.
_SUBPROC = r"""
import os, sys
os.environ.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
assert "litellm" not in sys.modules
import aiforge_core  # noqa: F401  -- must install the default first
assert "litellm" not in sys.modules, (
    "aiforge_core imported litellm itself, so the default landed too late")

import httpx
calls = []
def _record(url, *a, **k):
    calls.append(str(url))
    raise RuntimeError("network blocked by the test")
httpx.get = _record

import litellm
assert len(litellm.model_cost) > 100, "the bundled backup map must still load"
assert not calls, "litellm fetched the cost map over the network: %r" % (calls,)
print("OK", len(litellm.model_cost))
"""


@pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("litellm") is None,
    reason="litellm not installed")
def test_litellm_import_makes_no_network_call():
    """The real proof: import litellm for real with every httpx GET wired to
    raise, in a clean interpreter so import order is honest."""
    proc = subprocess.run(
        [sys.executable, "-c", _SUBPROC], cwd=str(_REPO),
        capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr[-3000:]}"
    assert proc.stdout.startswith("OK "), proc.stdout
