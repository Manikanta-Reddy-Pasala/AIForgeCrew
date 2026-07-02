"""Change 2 — robust fail-OPEN structured-output parsers.

A malformed local-model REJECT must not silently become a PASS just
because the raw text has prose around the JSON. Both parsers now try a
brace-balanced extractor before their fail-open default; the default is
the LAST resort, only reached on genuinely unparseable output.
"""
from __future__ import annotations

from aiforge_core.runtime import pr_reviewer
from aiforge_core.runtime.parallel_stages import _coerce_verdict


# ── parallel_stages._coerce_verdict ────────────────────────────────────

def test_coerce_verdict_prose_wrapped_reject() -> None:
    r = _coerce_verdict('Here is my take: {"verdict":"reject","reason":"x"}')
    assert r.get("verdict") == "reject"


def test_coerce_verdict_plain_dict() -> None:
    assert _coerce_verdict({"verdict": "reject"})["verdict"] == "reject"


def test_coerce_verdict_plain_json() -> None:
    assert _coerce_verdict('{"verdict":"reject"}')["verdict"] == "reject"


def test_coerce_verdict_fenced_reject() -> None:
    r = _coerce_verdict('```json\n{"verdict":"reject","rationale":"nope"}\n```')
    assert r["verdict"] == "reject"


def test_coerce_verdict_trailing_prose_reject() -> None:
    r = _coerce_verdict('{"verdict":"reject"} — see the missing lock above.')
    assert r["verdict"] == "reject"


def test_coerce_verdict_unparseable_fails_open() -> None:
    # genuinely no JSON → the fail-open default is defensible
    assert _coerce_verdict("the diff looks fine to me")["verdict"] == "pass"
    assert _coerce_verdict("")["verdict"] == "pass"


# ── pr_reviewer JSON extraction ────────────────────────────────────────

def test_pr_review_prose_json_parses() -> None:
    obj = pr_reviewer._extract_review_json(
        'Assessment: {"verdict":"request_changes","scope":1} looks risky')
    assert obj.get("verdict") == "request_changes"


def test_pr_review_fenced_json_parses() -> None:
    obj = pr_reviewer._extract_review_json(
        '```json\n{"verdict":"approve","scope":2}\n```')
    assert obj.get("verdict") == "approve"


def test_pr_review_garbage_empties() -> None:
    assert pr_reviewer._extract_review_json("no json here") == {}
    assert pr_reviewer._extract_review_json("") == {}
