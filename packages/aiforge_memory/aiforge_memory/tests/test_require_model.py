"""No default LLM model: an unset AIFORGE_CODEMEM_LM_MODEL is a clear error.

A baked-in model id made the LLM server load a model the operator never
configured. Every LLM call site now asks ``require_model`` — and a missing
model stops the run instead of reading as a per-item ``llm_error`` (which the
symbol pass would turn into "restart the LLM server").
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiforge_memory.features.file import extract as file_extract
from aiforge_memory.features.repo import extract as repo_extract
from aiforge_memory.features.service import extract as service_extract
from aiforge_memory.features.symbol import summarise as symbol_summarise
from aiforge_memory.features.symbol.extract import WalkedFile
from aiforge_memory.llm_compat import LmModelUnset, require_model


def test_require_model_passes_a_configured_model():
    assert require_model("m", "x") == "m"


def test_require_model_names_the_fix():
    with pytest.raises(LmModelUnset, match="AIFORGE_CODEMEM_LM_MODEL"):
        require_model("", "repo summary")


@pytest.mark.parametrize("call", [
    lambda: repo_extract._call_llm("p", system="s", user="u"),
    lambda: service_extract._call_llm("p", system="s", user="u"),
    lambda: file_extract._call_llm("c", path="a.py", lang="python"),
    lambda: symbol_summarise._call_llm(body="b", signature="", doc="",
                                       lang="python", path="a.py",
                                       fqname="a.f"),
], ids=["repo", "service", "file", "symbol"])
def test_every_call_site_refuses_without_a_model(monkeypatch, call):
    for mod in (repo_extract, service_extract, file_extract, symbol_summarise):
        monkeypatch.setattr(mod, "DEFAULT_MODEL", "")
    with pytest.raises(LmModelUnset):
        call()


def test_file_pass_stops_instead_of_skipping(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    wf = WalkedFile(repo="r", path="a.py", hash="h", lang="python", lines=1)
    monkeypatch.setattr(file_extract, "_summarize_one", lambda *a: (
        _ for _ in ()).throw(LmModelUnset("no model")))
    with pytest.raises(LmModelUnset):
        file_extract.summarize_files([wf], repo="r", repo_root=tmp_path)


def test_symbol_pass_stops_instead_of_counting_an_llm_error(monkeypatch):
    wf = SimpleNamespace(lang="python", path="a.py")
    sym = SimpleNamespace(fqname="a.f", line_start=1, line_end=2,
                          signature="", doc_first_line="")
    monkeypatch.setattr(symbol_summarise, "_call_llm", lambda **k: (
        _ for _ in ()).throw(LmModelUnset("no model")))
    with pytest.raises(LmModelUnset):
        symbol_summarise._process_one(wf, sym, repo="r",
                                      file_bytes=b"def f():\n    return 1\n")
