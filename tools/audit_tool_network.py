"""CI guard: no non-allowed tool handler may import a network library.

Usage: python3 tools/audit_tool_network.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Repo root resolution so this works from anywhere.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes.tools import build_default_registry  # noqa: E402
from aiforge_core.safety import assert_no_network_tools  # noqa: E402


def main() -> int:
    status = 0
    for role in ("em", "tester", "sr-developer", "sr-architect"):
        reg = build_default_registry(ROOT, role)
        try:
            assert_no_network_tools(reg)
        except RuntimeError as e:
            print(f"FAIL {role}:\n{e}", file=sys.stderr)
            status = 1
    if status == 0:
        print("No network-capable tools detected in any role registry.")
    return status


if __name__ == "__main__":
    sys.exit(main())
