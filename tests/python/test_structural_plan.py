"""Tests for ``aiforge_core.runtime.structural_plan``.

The heuristic plan-builder converts a mega-ticket body's explicit path
listing into the same JSON shape an external Architect would produce
(``{tree, symbols, imports}``). It feeds two surfaces:

1. The seed prompt — the Doer reads a Markdown rendering directly
2. ADK session state ``structural_plan`` — Doer can introspect at runtime

These tests don't drive ADK; they exercise the parser + renderer in
isolation against synthetic ticket bodies.
"""
from __future__ import annotations

from aiforge_core.runtime import structural_plan


# ─── extract_paths ────────────────────────────────────────────────────


def test_extract_paths_picks_up_listed_java_files():
    body = """
    Required files:
    1. src/main/java/com/pos/audit/AuditEvent.java
    2. src/main/java/com/pos/audit/AuditService.java
    3. src/main/java/com/pos/audit/AuditController.java
    """
    paths = structural_plan.extract_paths(body)
    assert paths == [
        "src/main/java/com/pos/audit/AuditEvent.java",
        "src/main/java/com/pos/audit/AuditService.java",
        "src/main/java/com/pos/audit/AuditController.java",
    ]


def test_extract_paths_handles_backtick_wrapping():
    body = "Edit `src/foo/bar.py` and `src/foo/baz.py`."
    paths = structural_plan.extract_paths(body)
    assert "src/foo/bar.py" in paths
    assert "src/foo/baz.py" in paths


def test_extract_paths_dedupes():
    body = """
    `src/main/foo.py` is the entry point.
    See also src/main/foo.py for details.
    """
    paths = structural_plan.extract_paths(body)
    assert paths.count("src/main/foo.py") == 1


def test_extract_paths_filters_short_paths():
    """README.md, setup.py, pom.xml at root level are not source paths."""
    body = "See README.md and setup.py for context."
    paths = structural_plan.extract_paths(body)
    # README.md doesn't match a known source ext; setup.py matches but
    # has only 1 path component, so filtered out.
    assert "setup.py" not in paths
    assert "README.md" not in paths


def test_extract_paths_filters_unknown_extensions():
    body = "config.yaml, deployment.tf, docker-compose.yml are infra."
    paths = structural_plan.extract_paths(body)
    assert paths == []


def test_extract_paths_preserves_listing_order():
    """Ticket bodies usually list paths in dependency order — preserve it."""
    body = """
    Phase 1: src/main/java/com/x/Model.java
    Phase 2: src/main/java/com/x/Dao.java
    Phase 3: src/main/java/com/x/Service.java
    Phase 4: src/main/java/com/x/Controller.java
    """
    paths = structural_plan.extract_paths(body)
    assert paths[0].endswith("Model.java")
    assert paths[-1].endswith("Controller.java")


def test_extract_paths_polyglot():
    body = """
    Backend: src/main/kotlin/foo/Bar.kt and src/main/scala/Baz.scala
    Frontend: web/src/components/Quux.tsx and web/src/utils/wibble.ts
    """
    paths = structural_plan.extract_paths(body)
    assert "src/main/kotlin/foo/Bar.kt" in paths
    assert "web/src/components/Quux.tsx" in paths


def test_extract_paths_empty_body():
    assert structural_plan.extract_paths("") == []
    assert structural_plan.extract_paths(None) == []


# ─── build_plan ───────────────────────────────────────────────────────


def test_build_plan_returns_none_when_too_few_paths():
    """Below the 3-path floor, the plan adds noise without value."""
    body = "Only one file: src/main/foo.py"
    assert structural_plan.build_plan(body) is None


def test_build_plan_returns_dict_when_paths_present():
    body = """
    1. src/foo/a.py
    2. src/foo/b.py
    3. src/foo/c.py
    """
    plan = structural_plan.build_plan(body)
    assert plan is not None
    assert plan["tree"] == ["src/foo/a.py", "src/foo/b.py", "src/foo/c.py"]
    # Symbols / imports are empty by design — heuristic doesn't infer them.
    assert plan["symbols"] == {}
    assert plan["imports"] == {}
    assert plan["source"] == "ticket_body_heuristic"


def test_build_plan_matches_architect_contract_shape():
    """Plan dict must have the keys the Architect prompt promises so a
    future real-Architect plan and a heuristic plan are interchangeable."""
    body = "src/main/a.py src/main/b.py src/main/c.py"
    plan = structural_plan.build_plan(body)
    assert plan is not None
    for key in ("tree", "symbols", "imports", "source"):
        assert key in plan, f"missing key: {key}"


# ─── render_for_prompt ────────────────────────────────────────────────


def test_render_for_prompt_emits_markdown_block():
    plan = {
        "tree": ["src/foo/a.py", "src/foo/b.py", "src/foo/c.py"],
        "symbols": {},
        "imports": {},
        "source": "ticket_body_heuristic",
    }
    md = structural_plan.render_for_prompt(plan)
    assert "## Canonical file tree" in md
    assert "`src/foo/a.py`" in md
    assert "`src/foo/b.py`" in md
    # Anti-drift rule should be in the rendered text.
    assert "feature/" in md or "different package paths" in md


def test_render_for_prompt_empty_plan_returns_empty_string():
    """No paths means no rendering — caller can concatenate safely."""
    assert structural_plan.render_for_prompt({}) == ""
    assert structural_plan.render_for_prompt({"tree": []}) == ""
    assert structural_plan.render_for_prompt(None) == ""


# ─── ONE-1 audit subsystem regression — the canary spec ───────────────


def test_one1_audit_spec_yields_correct_tree():
    """The exact mega-ticket body that drove ONE-1 must produce a tree
    that includes every audit/* path the spec listed. This guards
    against regex regressions that would silently break the heuristic."""
    body = """
    9. src/main/java/com/pos/backend/audit/dao/AuditEventDao.java
    10. src/main/java/com/pos/backend/audit/dao/AuditExportDao.java
    11. src/main/java/com/pos/backend/audit/dao/AuditAlertDao.java
    12. src/main/java/com/pos/backend/audit/repository/AuditEventRepository.java
    23. src/main/java/com/pos/backend/audit/controller/AuditEventController.java
    """
    plan = structural_plan.build_plan(body)
    assert plan is not None
    assert any("audit/dao/" in p for p in plan["tree"])
    assert any("audit/repository/" in p for p in plan["tree"])
    assert any("audit/controller/" in p for p in plan["tree"])
    # No drift paths — heuristic doesn't invent feature/audit/.
    assert not any("feature/audit" in p for p in plan["tree"])
