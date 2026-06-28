"""Unit tests for translator retrieval-quality helpers — RRF, rerank,
camelCase tokenizer, query expansion, path-prior. No driver / sidecar /
LLM dependencies."""
from __future__ import annotations

from aiforge_memory.query import translator as t


def test_camel_split_camel_case() -> None:
    assert t._camel_split("BusinessProductsController") == [
        "Business", "Products", "Controller",
    ]
    assert t._camel_split("ABCService") == ["ABC", "Service"]
    assert t._camel_split("snake_case_token") == ["snake", "case", "token"]
    assert t._camel_split("") == []


def test_tokenize_for_fulltext_dedupes_and_lowercases() -> None:
    out = t._tokenize_for_fulltext("BusinessProductsController productsService")
    assert "Business" in out and "Products" in out and "Controller" in out
    assert "Service" in out
    # `products` (lower) seen first; second occurrence must be dropped
    assert sum(1 for x in out if x.lower() == "products") == 1


def test_tokenize_drops_short_tokens() -> None:
    assert t._tokenize_for_fulltext("a bb ccc") == ["ccc"]


def test_expand_query_appends_synonyms() -> None:
    expanded = t._expand_query("add JWT auth")
    assert "JWT" in expanded
    assert "authentication" in expanded
    assert "token" in expanded


def test_expand_query_no_synonym_returns_original() -> None:
    assert t._expand_query("xyzzy frobnicate") == "xyzzy frobnicate"


def test_rrf_fuse_ranks_overlap_first() -> None:
    out = t._rrf_fuse(
        ranked_lists=[
            ["a", "b", "c"],
            ["c", "d", "a"],
        ],
    )
    # `a` and `c` appear in both lists → must rank above `b` and `d`
    assert out.index("a") < out.index("b")
    assert out.index("c") < out.index("d")


def test_rrf_fuse_path_prior_bonus_breaks_ties() -> None:
    out = t._rrf_fuse(
        ranked_lists=[["x.java", "y.java"]],
        path_prior={"y.java": 0.5},
    )
    # path-prior bonus on y must override its lower base rank
    assert out[0] == "y.java"


def test_path_prior_controller_test_dto() -> None:
    paths = [
        "src/main/java/X.java",
        "src/main/java/XController.java",
        "src/main/java/XService.java",
        "src/main/java/XDto.java",
        "src/test/java/XTest.java",
    ]
    bonus = t._path_prior("add controller endpoint", paths)
    assert bonus.get("src/main/java/XController.java", 0) > 0
    bonus = t._path_prior("test sales", paths)
    assert bonus.get("src/test/java/XTest.java", 0) > 0
    bonus = t._path_prior("dto request", paths)
    assert bonus.get("src/main/java/XDto.java", 0) > 0


def test_path_prior_no_cues_returns_empty() -> None:
    assert t._path_prior("xyz frobnicate", ["a.java", "b.java"]) == {}


def test_escape_lucene_specials() -> None:
    assert t._escape_lucene("foo+bar") == "foo\\+bar"
    assert t._escape_lucene("a:b/c") == "a\\:b\\/c"
