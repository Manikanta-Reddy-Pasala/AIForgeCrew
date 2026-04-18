import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError

SCHEMA_PATH = Path("tools/schemas/permissions.schema.json")


@pytest.fixture
def validator():
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_schema_file_exists():
    assert SCHEMA_PATH.exists()


def test_valid_permissions_passes(validator):
    doc = yaml.safe_load("""
role: em
reports_to: human
model_location: cloud
can:
  read_src: false
  write_src: false
  read_tests: false
  write_tests: false
  git_commit: false
  git_create_mr: false
  ticket_comment: true
  ticket_assign: true
  hermes_execute: false
  mem0_project_write: true
  network_fetch: false
""")
    validator.validate(doc)


def test_missing_role_fails(validator):
    doc = yaml.safe_load("can: {}")
    with pytest.raises(ValidationError):
        validator.validate(doc)


def test_unknown_role_fails(validator):
    doc = yaml.safe_load("role: ceo\ncan: {}")
    with pytest.raises(ValidationError):
        validator.validate(doc)
