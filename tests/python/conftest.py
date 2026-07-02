"""Suite-wide test defaults.

C2: the /v1/models context-window auto-detect probe fires a GET (2-3s timeout
when no endpoint is up) on the pure-default config, thrashing the suite. Disable
it by default here so tests don't probe the network. Individual tests that WANT
to exercise the probe/enabled path override with ``monkeypatch.setenv`` (which
restores this default afterwards). ``setdefault`` means an explicit environment
value from the operator still wins.
"""
from __future__ import annotations

import os

os.environ.setdefault("AIFORGE_AUTODETECT_CTX", "0")
