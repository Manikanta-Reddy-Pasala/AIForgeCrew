"""Config isolation for the API tests.

``tests/python/conftest.py`` redirects AIFORGE_CONFIG_DIR so the suite cannot
read or write the operator's real ``~/.aiforge``; this directory had no such
guard. It matters more now that a stored ``llm_max_rpm`` would THROTTLE the
run: runtime_settings resolves stored → env → default, so the root conftest's
``AIFORGE_LLM_MAX_RPM=0`` is silently outranked by a value someone typed into
the Settings UI on their own machine.

Scoped to this directory (not the root conftest) so tests/python keeps the
isolation it already arranges for itself.
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("AIFORGE_CONFIG_DIR",
                      tempfile.mkdtemp(prefix="aiforge-api-tests-"))
