"""Topic ADMISSION control — the guard that stopped 142 topic briefs (a third
of them holding one fact, plus magnets like `code`/`data`/`tmp`/`m`) from
forming and poisoning recall."""
from __future__ import annotations

import pytest

from aiforge_core.memory.md_store import _topics


@pytest.mark.parametrize("slug", [
    "m", "mx", "nd", "na2", "tw2", "jt2",        # eval-run junk
    "code", "data", "file", "build", "tmp",       # recall magnets
    "test-data", "code-file",                     # every word generic
    "java", "python", "cpp",                      # bare language names
    "", "---", "123",                             # degenerate
])
def test_junk_slugs_are_refused(slug):
    assert _topics.topic_ok(slug) is False


@pytest.mark.parametrize("slug", [
    "data-sync", "change-stream-consumer", "billing-pipeline",
    "message-retry-service", "kube-hetzner",
])
def test_real_subjects_are_admitted(slug):
    assert _topics.topic_ok(slug) is True


def test_admit_snaps_before_judging():
    # admit() runs the lexical family snapper first, so an extension of an
    # existing topic collapses onto it instead of minting a sibling file.
    seen = {}

    def snap(s):
        seen["called"] = s
        return "data-sync"

    assert _topics.admit("data-sync-retries", snap) == "data-sync"
    assert seen["called"] == "data-sync-retries"


def test_admit_refuses_generic_even_after_snap():
    assert _topics.admit("code", lambda s: s) is None
    assert _topics.admit("m", lambda s: s) is None


def test_admit_keeps_an_existing_topic_that_would_fail_today(monkeypatch):
    # Refusing a topic that already holds facts would strand them — an existing
    # brief is admitted regardless of the current naming rules.
    monkeypatch.setattr(_topics, "existing_topics", lambda: ["tmp"])
    assert _topics.admit("tmp", lambda s: s) == "tmp"


def test_semantic_paths_are_off_on_the_hash_backend(monkeypatch):
    # The default hash embedder is a bag-of-tokens projection: two briefs that
    # share boilerplate score ~1.0 regardless of subject. Auto-snapping on it
    # silently fuses unrelated topics, so it must stay disabled.
    monkeypatch.delenv("AIFORGE_TOPIC_SEMANTIC", raising=False)
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    assert _topics.semantic_ready() is False
    hit, shortlist = _topics.snap_by_similarity(
        "retry backoff on the sync queue", ["data-sync", "auth-tokens"])
    assert hit is None                    # never auto-assigns
    assert "data-sync" in shortlist       # still suggests candidates


def test_lexical_shortlist_ranks_by_word_overlap():
    out = _topics._lexical_shortlist(
        "sync retries use exponential backoff",
        ["auth-tokens", "data-sync-retries", "kube-hetzner"], 2)
    assert out[0] == "data-sync-retries"


def test_existing_topics_excludes_repo_and_shared_briefs(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path))
    from aiforge_core.memory import md_store as m
    m._brief_upsert("shared", "a global rule")
    m._brief_upsert("oneshell-pos", "a repo fact")
    m._brief_upsert("data-sync", "a topic fact")
    monkeypatch.setattr(_topics, "_repo_brief_names", lambda: {"oneshell-pos"})
    assert _topics.existing_topics() == ["data-sync"]
