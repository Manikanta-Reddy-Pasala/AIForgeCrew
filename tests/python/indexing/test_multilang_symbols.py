"""Multi-language symbol ingest via aider tag queries.

The generic tag-based extractor (`_parse_via_tags`) turns any language aider
ships a tags query for (python / kotlin / cpp / c / typescript / tsx / …) into
the same ``FileParseResult`` shape the Java walker emits, so the existing
Neo4j writers stay untouched. These tests pin:

  * per-language def extraction + kind classification (class vs method)
  * ref → caller-by-line linking into ``call_simples``
  * def dedup (aider emits duplicate def tags)
  * ``ingest_repo`` dispatch: .java → rich walker, others → tags
  * soft-fail when aider is unavailable
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aiforge_core.indexing import treesitter_ingest as tsi


def _lang(fpath: Path) -> str:
    from grep_ast import filename_to_lang
    return filename_to_lang(str(fpath))


def _parse(tmp_path: Path, name: str, body: str):
    f = tmp_path / name
    f.write_text(body)
    src = f.read_bytes()
    return tsi._parse_via_tags(f, src, "r", "sha", _lang(f))


def _names(result) -> set[str]:
    return {s.simple for s in result.symbols}


def _kind_of(result, simple: str) -> str:
    return next(s.kind for s in result.symbols if s.simple == simple)


def _callees(result) -> set[str]:
    return {callee for _caller, callee in result.call_simples}


def _assert_dedup(result):
    keys = [(s.simple, s.start_line) for s in result.symbols]
    assert len(keys) == len(set(keys)), f"duplicate def symbols: {keys}"
    fqns = [s.fqn for s in result.symbols]
    assert len(fqns) == len(set(fqns)), f"duplicate fqns: {fqns}"


# ─────────────── per-language extraction ───────────────

def test_python(tmp_path):
    res = _parse(
        tmp_path, "A.py",
        "class Foo:\n"
        "    def bar(self):\n"
        "        return self.baz()\n"
        "def baz():\n"
        "    return 1\n",
    )
    assert {"Foo", "bar", "baz"} <= _names(res)
    assert _kind_of(res, "Foo") == "class"
    assert _kind_of(res, "bar") == "method"
    assert "baz" in _callees(res)
    # the call to baz sits inside bar → caller should be a bar-ish symbol
    callers = {caller for caller, callee in res.call_simples if callee == "baz"}
    assert any(c.endswith("bar") for c in callers), callers
    assert res.file.language == "python"
    _assert_dedup(res)


def test_kotlin(tmp_path):
    res = _parse(
        tmp_path, "B.kt",
        "class MarsController {\n"
        "    fun handle(): String {\n"
        "        return lookup()\n"
        "    }\n"
        "}\n",
    )
    assert {"MarsController", "handle"} <= _names(res)
    assert _kind_of(res, "MarsController") == "class"
    assert "lookup" in _callees(res)
    _assert_dedup(res)


def test_cpp(tmp_path):
    res = _parse(
        tmp_path, "C.cpp",
        "class Widget {\n"
        "public:\n"
        "    int run() { return calc(); }\n"
        "};\n"
        "int calc() { return 2; }\n",
    )
    assert {"Widget", "run", "calc"} <= _names(res)
    assert _kind_of(res, "Widget") == "class"
    assert res.call_simples, "cpp should backfill at least one ref"
    _assert_dedup(res)


def test_tsx(tmp_path):
    res = _parse(
        tmp_path, "D.tsx",
        "import { useState } from 'react';\n"
        "function App() {\n"
        "    const [x] = useState(0);\n"
        "    return x;\n"
        "}\n",
    )
    assert "App" in _names(res)
    assert "useState" in _callees(res)
    _assert_dedup(res)


def test_c(tmp_path):
    res = _parse(
        tmp_path, "E.c",
        "int add(int a, int b) { return a + b; }\n"
        "int main() { return add(1, 2); }\n",
    )
    assert {"add", "main"} <= _names(res)
    assert res.call_simples, "c should backfill at least one ref"
    _assert_dedup(res)


# ─────────────── ingest_repo dispatch ───────────────

def test_ingest_repo_dispatches_java_and_tags(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "A.java").write_text(
        "package p;\npublic class A {\n  int f(){ return g(); }\n  int g(){ return 1; }\n}\n")
    (repo / "src" / "a.py").write_text(
        "class Py:\n    def m(self):\n        return h()\ndef h():\n    return 1\n")
    (repo / "src" / "b.kt").write_text(
        "class Kt {\n    fun k(): Int { return q() }\n}\n")

    # Spy the java walker (must still run for .java) delegating to the real one.
    real_java = tsi._parse_java_file
    java_calls: list[str] = []

    def spy_java(path, src, repo_name, sha1):
        java_calls.append(str(path))
        return real_java(path, src, repo_name, sha1)

    monkeypatch.setattr(tsi, "_parse_java_file", spy_java)

    # Spy the tags path.
    real_tags = tsi._parse_via_tags
    tag_calls: list[str] = []

    def spy_tags(fpath, src, repo_name, sha1, lang, repo_root=None):
        tag_calls.append(str(fpath))
        return real_tags(fpath, src, repo_name, sha1, lang, repo_root=repo_root)

    monkeypatch.setattr(tsi, "_parse_via_tags", spy_tags)

    # Capture Neo4j writes without touching a DB.
    payload_paths: list[str] = []
    monkeypatch.setattr(
        tsi, "_write_file_payload",
        lambda session, parsed, stats: payload_paths.append(parsed.file.path))
    monkeypatch.setattr(tsi, "_resolve_edges", lambda *a, **k: None)

    driver = MagicMock(name="neo4j_driver")
    # A fresh file (no prior sha1) — make the existing-sha1 lookup return None.
    session = driver.session.return_value.__enter__.return_value
    session.run.return_value.single.return_value = None

    stats = tsi.ingest_repo(driver, repo, "r")

    assert stats.files_seen == 3
    assert java_calls == [str(repo / "src" / "A.java")]
    assert sorted(Path(p).name for p in tag_calls) == ["a.py", "b.kt"]
    assert len(payload_paths) == 3  # one payload per parsed file


# ─────────────── degradation ───────────────

def test_aider_unavailable_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(tsi, "_REPOMAP", None, raising=False)
    monkeypatch.setattr(tsi, "_REPOMAP_FAILED", False, raising=False)

    def boom():
        raise ImportError("aider gone")

    monkeypatch.setattr(tsi, "_import_aider", boom)

    f = tmp_path / "A.py"
    f.write_text("class Foo:\n    def bar(self):\n        return 1\n")
    res = tsi._parse_via_tags(f, f.read_bytes(), "r", "sha", "python")

    assert res.symbols == []
    assert res.call_simples == []
    assert res.file.language == "python"
