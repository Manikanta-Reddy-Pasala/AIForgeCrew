# tools/check_permission_matrix.py
"""Cross-check agents/*/permissions.yml against DESIGN.md §5.2 canonical matrix.

This script is the source of truth that DESIGN.md §5.2 cannot silently drift
from the YAML files. If DESIGN changes a cell, this matrix must update too.
"""
from __future__ import annotations

import sys
from fnmatch import fnmatch
from pathlib import Path

import yaml

CANONICAL: dict[str, dict[str, bool]] = {
    "em": {
        "read_src": False, "write_src": False,
        "read_tests": False, "write_tests": False,
        "git_commit": False, "git_create_mr": False,
        "ticket_comment": True, "ticket_assign": True,
        "hermes_execute": False, "mem0_project_write": True,
        "network_fetch": False,
    },
    "tester": {
        "read_src": True, "write_src": False,
        "read_tests": True, "write_tests": True,
        "git_commit": True, "git_create_mr": False,
        "ticket_comment": True, "ticket_assign": True,
        "hermes_execute": True, "mem0_project_write": False,
        "network_fetch": True,    # allowlisted domains only — see aiforge_core/net.py
    },
    "sr-developer": {
        "read_src": True, "write_src": True,
        "read_tests": True, "write_tests": False,
        "git_commit": True, "git_create_mr": False,
        "ticket_comment": True, "ticket_assign": False,
        "hermes_execute": True, "mem0_project_write": False,
        "network_fetch": True,    # allowlisted domains only
    },
    "sr-architect": {
        "read_src": True, "write_src": False,
        "read_tests": True, "write_tests": False,
        "git_commit": False, "git_create_mr": True,
        "ticket_comment": True, "ticket_assign": True,
        "hermes_execute": False, "mem0_project_write": True,
        "network_fetch": False,
    },
}


def load_role_yaml(role: str) -> dict:
    return yaml.safe_load(Path(f"agents/{role}/permissions.yml").read_text())


def check_matrix() -> list[str]:
    errors: list[str] = []
    for role, expected in CANONICAL.items():
        doc = load_role_yaml(role)
        actual = doc["can"]
        for capability, expected_value in expected.items():
            if actual.get(capability) != expected_value:
                errors.append(
                    f"{role}.{capability}: YAML={actual.get(capability)} expected={expected_value}"
                )
    return errors


def check_no_role_writes_blocked_paths() -> list[str]:
    rules = yaml.safe_load(Path("security/file-access-rules.yml").read_text())
    blocked = yaml.safe_load(Path("security/blocked-paths.yml").read_text())["globally_blocked"]
    errors: list[str] = []
    for role, acl in rules["roles"].items():
        for write_glob in acl["write"]:
            for block_glob in blocked:
                if fnmatch(write_glob, block_glob) or fnmatch(block_glob, write_glob):
                    errors.append(f"{role} write glob {write_glob!r} overlaps blocked {block_glob!r}")
    return errors


def main() -> int:
    errors = check_matrix() + check_no_role_writes_blocked_paths()
    if errors:
        print("PERMISSION MATRIX DRIFT:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("Permission matrix OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
