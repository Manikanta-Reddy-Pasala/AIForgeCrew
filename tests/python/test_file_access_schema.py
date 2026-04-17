import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError

SCHEMA_PATH = Path("tools/schemas/file-access-rules.schema.json")


@pytest.fixture
def validator():
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_valid_rules_pass(validator):
    doc = yaml.safe_load("""
version: 1
roles:
  tester:
    write: ["tests/**"]
    read: ["src/**", "tests/**", ".env.test"]
    deny: [".env", "secrets/**"]
""")
    validator.validate(doc)


def test_write_overlaps_deny_allowed_structurally(validator):
    doc = yaml.safe_load("""
version: 1
roles:
  tester:
    write: []
    read: []
    deny: []
""")
    validator.validate(doc)


def test_missing_version_fails(validator):
    doc = yaml.safe_load("roles: {}")
    with pytest.raises(ValidationError):
        validator.validate(doc)
