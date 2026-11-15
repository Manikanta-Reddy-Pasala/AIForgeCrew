"""A message stating several rules stores every one of them, not just the first.

Live case (real PosClientBackend job): "Two rules ... 1. run Maven offline ...
2. PosClientBackend needs JDK 24 ... JAVA_HOME=..." captured only rule 1; the
JDK rule was lost, so a later session had to rediscover it.
"""
import json

from aiforge_core.runtime import rule_capture as rc


def _raw(**kw):
    base = {"category": "rule", "scope": "project", "canonical": "run maven offline",
            "confidence": 0.9, "task_present": True, "triggers": []}
    base.update(kw)
    return json.dumps(base)


def test_more_items_are_parsed_with_exact_values():
    c = rc._parse_classification(_raw(more=[
        {"category": "memory", "scope": "project", "confidence": 0.9,
         "canonical": "JDK 24: JAVA_HOME=/home/ai/jdk24/jdk-24.0.2+12"}]))
    assert c["canonical"] == "run maven offline"
    assert [m["canonical"] for m in c["more"]] == [
        "JDK 24: JAVA_HOME=/home/ai/jdk24/jdk-24.0.2+12"]
    assert c["more"][0]["task_present"] is True


def test_bad_duplicate_and_missing_scope_items():
    c = rc._parse_classification(_raw(more=[
        {"category": "bogus", "canonical": "x", "confidence": 1},
        {"category": "rule", "canonical": "Run Maven Offline", "confidence": 1},
        {"category": "rule", "canonical": "never push", "confidence": 1},
        "not an object"]))
    assert [m["canonical"] for m in c["more"]] == ["never push"]
    assert c["more"][0]["scope"] == "project"      # inherits the first's scope


def test_no_more_key_is_an_empty_list():
    assert rc._parse_classification(_raw())["more"] == []


def test_classify_drops_low_confidence_extras_and_promotes(monkeypatch):
    monkeypatch.setattr(rc, "_llm_complete", lambda *a, **k: _raw(
        category="none", canonical="", more=[
            {"category": "rule", "canonical": "weak", "confidence": 0.1},
            {"category": "rule", "canonical": "strong", "confidence": 0.95}]))
    c = rc.classify("1. weak 2. strong")
    assert c["canonical"] == "strong" and c["more"] == []


def test_store_all_stores_each(monkeypatch):
    seen = []
    monkeypatch.setattr(rc, "store",
                        lambda c, **k: seen.append(c["canonical"]) or {"id": c["canonical"]})
    out = rc.store_all({"category": "rule", "canonical": "a",
                        "more": [{"category": "memory", "canonical": "b"}]})
    assert seen == ["a", "b"] and [o["id"] for o in out] == ["a", "b"]


def test_one_off_directive_is_session_scope():
    c = rc._parse_classification(_raw(canonical="Use the pcb-bugfix workflow for this."))
    assert c["scope"] == "session"
    for lasting in ("Always use the pcb-bugfix workflow for this repo",
                    "From now on use the pcb-bugfix workflow for this kind of bug",
                    "run maven offline"):
        assert rc._parse_classification(_raw(canonical=lasting))["scope"] == "project"
