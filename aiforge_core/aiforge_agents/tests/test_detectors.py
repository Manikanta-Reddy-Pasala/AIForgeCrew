"""Real-detector tests — F-001 through F-009 actually catching things."""
from __future__ import annotations

from unittest.mock import MagicMock

from aiforge_core.aiforge_agents.runtime import detectors as d
from aiforge_core.aiforge_agents.runtime import recovery


# ─────────── LoopDetector ────────────────────────────────────────────

def test_loop_detector_no_trip_on_two() -> None:
    ld = d.LoopDetector(window=3)
    assert ld.record("same") is None
    assert ld.record("same") is None


def test_loop_detector_trips_on_three_identical() -> None:
    ld = d.LoopDetector(window=3, mode_id="F-007")
    ld.record("err")
    ld.record("err")
    hit = ld.record("err")
    assert hit is not None
    assert hit.mode.id == "F-007"


def test_loop_detector_does_not_trip_on_progress() -> None:
    ld = d.LoopDetector(window=3)
    ld.record("a")
    ld.record("b")
    assert ld.record("c") is None


def test_loop_detector_reset() -> None:
    ld = d.LoopDetector(window=3)
    ld.record("x"); ld.record("x")
    ld.reset()
    assert ld.record("x") is None  # buffer cleared


# ─────────── HallucinatedImport ──────────────────────────────────────

def test_extract_imports_java() -> None:
    diff = "+ import com.example.PaymentService;\n+ import java.util.List;"
    found = d.extract_imports(diff)
    assert "com.example.PaymentService" in found
    assert "java.util.List" in found


def test_extract_imports_python_both_forms() -> None:
    diff = "from foo.bar import baz\nimport os.path"
    found = d.extract_imports(diff)
    assert "foo.bar" in found
    assert "os.path" in found


def test_hallucinated_import_jdk_passes() -> None:
    det = d.HallucinatedImportDetector(repo="r", driver=None)
    hits = det.check("import java.util.List;")
    assert hits == []


def test_hallucinated_import_unknown_blocks_without_driver() -> None:
    det = d.HallucinatedImportDetector(repo="r", driver=None)
    hits = det.check("import com.bogus.Made.Up;")
    assert len(hits) == 1
    assert hits[0].mode.id == "F-001"


def test_hallucinated_import_known_package_passes() -> None:
    det = d.HallucinatedImportDetector(
        repo="r", driver=None,
        known_packages={"com.bogus"},
    )
    hits = det.check("import com.bogus.Anything;")
    assert hits == []


def test_hallucinated_import_graph_resolves() -> None:
    fake_session = MagicMock()
    fake_session.run.return_value.single.return_value = {"g.path": "x"}
    fake_driver = MagicMock()
    fake_driver.session.return_value.__enter__.return_value = fake_session
    det = d.HallucinatedImportDetector(repo="r", driver=fake_driver)
    hits = det.check("import com.example.Service;")
    assert hits == []


def test_hallucinated_import_spring_lombok_swagger_pass() -> None:
    """Standard frameworks must not be flagged as hallucinations."""
    det = d.HallucinatedImportDetector(repo="r", driver=None)
    diff = (
        "+ import org.springframework.web.bind.annotation.RestController;\n"
        "+ import org.springframework.http.ResponseEntity;\n"
        "+ import lombok.RequiredArgsConstructor;\n"
        "+ import io.swagger.v3.oas.annotations.tags.Tag;\n"
        "+ import com.fasterxml.jackson.annotation.JsonProperty;\n"
        "+ import org.slf4j.Logger;\n"
    )
    assert det.check(diff) == []


def test_hallucinated_import_wildcard_strips_dot_star() -> None:
    """`import org.springframework.web.bind.annotation.*;` must pass."""
    det = d.HallucinatedImportDetector(repo="r", driver=None)
    assert det.check("+ import org.springframework.web.bind.annotation.*;") == []


def test_extract_imports_handles_static_imports() -> None:
    """`import static ...;` must capture the FQN, not the `static` keyword."""
    diff = (
        "+ import static org.junit.jupiter.api.Assertions.assertEquals;\n"
        "+ import static org.mockito.Mockito.*;\n"
    )
    found = d.extract_imports(diff)
    assert "static" not in found
    assert "org.junit.jupiter.api.Assertions.assertEquals" in found
    assert "org.mockito.Mockito" in found


def test_hallucinated_import_plan_create_sibling_passes() -> None:
    """Imports of files being created in this same plan are not hallucinated."""
    det = d.HallucinatedImportDetector(
        repo="r", driver=None,
        plan_create_fqns={
            "com.pos.backend.feature.ledger.LedgerCategoryService",
            "com.pos.backend.feature.ledger.LedgerCategoryDto",
        },
    )
    diff = (
        "+ import com.pos.backend.feature.ledger.LedgerCategoryService;\n"
        "+ import com.pos.backend.feature.ledger.LedgerCategoryDto;\n"
    )
    assert det.check(diff) == []


# ─────────── HallucinatedSymbol ──────────────────────────────────────

def test_hallucinated_symbol_jdk_passes() -> None:
    fake_driver = MagicMock()
    det = d.HallucinatedSymbolDetector(repo="r", driver=fake_driver)
    hits = det.check(["java.util.List", "javax.persistence.Entity"])
    assert hits == []


def test_hallucinated_symbol_unknown_blocks() -> None:
    fake_session = MagicMock()
    fake_session.run.return_value.single.return_value = None
    fake_driver = MagicMock()
    fake_driver.session.return_value.__enter__.return_value = fake_session
    det = d.HallucinatedSymbolDetector(repo="r", driver=fake_driver)
    hits = det.check(["com.example.MadeUp"])
    assert len(hits) == 1
    assert hits[0].mode.id == "F-002"


# ─────────── DiffContextHash ─────────────────────────────────────────

def test_diff_hash_match_passes() -> None:
    file_text = "line1\nline2\nline3\n"
    udiff = (
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+line2-modified\n"
        " line3\n"
    )
    assert d.DiffContextHashDetector.check(udiff=udiff, file_text=file_text) is None


def test_diff_hash_mismatch_blocks() -> None:
    file_text = "actual1\nactual2\nactual3\n"
    udiff = (
        "@@ -1,3 +1,3 @@\n"
        " bogus_context\n"
        "-x\n"
        "+y\n"
        " actual3\n"
    )
    hit = d.DiffContextHashDetector.check(udiff=udiff, file_text=file_text)
    assert hit is not None
    assert hit.mode.id == "F-003"


# ─────────── Plan depth ──────────────────────────────────────────────

def test_plan_depth_under_limit() -> None:
    plan = {"steps": [{} for _ in range(7)]}
    assert d.check_plan_depth(plan) is None


def test_plan_depth_over_limit() -> None:
    plan = {"steps": [{} for _ in range(15)]}
    hit = d.check_plan_depth(plan)
    assert hit is not None
    assert hit.mode.id == "F-006"


# ─────────── Token budget ────────────────────────────────────────────

def test_token_budget_under() -> None:
    assert d.check_token_budget(1500, expected=1000, multiplier=2.0) is None


def test_token_budget_over() -> None:
    hit = d.check_token_budget(2500, expected=1000, multiplier=2.0)
    assert hit is not None
    assert hit.mode.id == "F-009"


# ─────────── Recovery policy ────────────────────────────────────────

def test_recovery_block_and_retry_for_hallucinations() -> None:
    assert recovery.decide("F-001") == recovery.Action.BLOCK_AND_RETRY
    assert recovery.decide("F-002") == recovery.Action.BLOCK_AND_RETRY
    assert recovery.decide("F-003") == recovery.Action.BLOCK_AND_RETRY


def test_recovery_kgr_for_loops() -> None:
    assert recovery.decide("F-004") == recovery.Action.KGR_FALLBACK
    assert recovery.decide("F-007") == recovery.Action.KGR_FALLBACK
    assert recovery.decide("F-010") == recovery.Action.KGR_FALLBACK


def test_recovery_split_for_depth() -> None:
    assert recovery.decide("F-006") == recovery.Action.SPLIT_TICKET


def test_recovery_unknown_escalates() -> None:
    assert recovery.decide("F-999") == recovery.Action.ESCALATE_HUMAN
