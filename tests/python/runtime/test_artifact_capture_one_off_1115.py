"""A single bug fix is not saved as a library skill/workflow.

Live case: a real PosClientBackend turn fixed inventory-aging labels and
auto-saved a GLOBAL skill "Fix Inventory Aging Bucket Label Formatting".
"""
from aiforge_core.runtime.artifact_capture import one_off_fix

STEPS = [{"type": "tool", "name": "file_patch",
          "args": {"path": "src/main/java/a/InventoryAgingCalculator.java"}},
         {"type": "tool", "name": "multi_edit",
          "args": {"edits": [{"path": "src/test/java/a/InventoryAgingCalculatorTest.java"}]}}]


def test_fix_named_proposal_is_one_off():
    assert one_off_fix("Fix Inventory Aging Bucket Label Formatting", "", [], [])


def test_proposal_naming_an_edited_file_is_one_off():
    assert one_off_fix("Aging labels", "how InventoryAgingCalculator formats labels", [], STEPS)


def test_general_procedure_is_kept():
    assert not one_off_fix("Run PosClientBackend unit tests offline",
                           "JDK 24 + mvn -o test -Dtest=<Class>", ["maven"], STEPS)
