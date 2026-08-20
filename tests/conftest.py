"""Root test defaults — applies to every test directory, not just tests/python.

The operator's calls-per-minute ceiling (``llm_max_rpm``, default 5) is a real
throttle at the wire. Any suite that drives the transport more than five times
a minute would spend the rest of the run parked in the limiter's queue, so it
is off unless a test asks for it. ``setdefault`` keeps an explicit operator
value winning.
"""
from __future__ import annotations

import os

os.environ.setdefault("AIFORGE_LLM_MAX_RPM", "0")
