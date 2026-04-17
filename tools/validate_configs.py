# tools/validate_configs.py
"""Walk repo, validate every YAML under agents/ and security/ against schemas."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

SCHEMA_MAP: dict[str, Path] = {
    "agents/*/permissions.yml": Path("tools/schemas/permissions.schema.json"),
    "security/file-access-rules.yml": Path("tools/schemas/file-access-rules.schema.json"),
    "security/blocked-paths.yml": Path("tools/schemas/blocked-paths.schema.json"),
}


def load_validator(schema_path: Path) -> Draft202012Validator:
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def main() -> int:
    errors: list[str] = []
    for pattern, schema_path in SCHEMA_MAP.items():
        validator = load_validator(schema_path)
        for path in Path(".").glob(pattern):
            try:
                doc = yaml.safe_load(path.read_text())
            except yaml.YAMLError as exc:
                errors.append(f"{path}: YAML parse error: {exc}")
                continue
            for err in validator.iter_errors(doc):
                loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
                errors.append(f"{path} at {loc}: {err.message}")

    if errors:
        print("VALIDATION FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("All configs valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
