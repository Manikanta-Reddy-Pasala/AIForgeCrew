import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError

SCHEMA_PATH = Path("tools/schemas/blocked-paths.schema.json")


@pytest.fixture
def validator():
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_valid_blocked_paths(validator):
    doc = yaml.safe_load("""
version: 1
globally_blocked:
  - ".env"
  - ".env.prod"
  - "secrets/**"
  - "config/prod/**"
  - ".github/**"
""")
    validator.validate(doc)


def test_missing_globally_blocked_fails(validator):
    doc = yaml.safe_load("version: 1")
    with pytest.raises(ValidationError):
        validator.validate(doc)
