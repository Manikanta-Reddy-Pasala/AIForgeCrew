"""The RAG-eval judge and embedder say who they are.

Both are the OpenAI SDK underneath, and the SDK stamps its OWN User-Agent on
every request unless ``default_headers`` overrides it. These two were the one
pair of senders a gateway could not attribute to AIForge at all — they had no
header of any kind, so their traffic filed as "OpenAI/Python <ver>" beside the
structured path's.

``langchain_openai`` and ``ragas`` are optional extras and are not installed on
every box. Stubbing them keeps this hermetic rather than skipping: a test that
disappears when a dependency is absent is how the original attribution bug
survived a green suite, and skipping here would reproduce that exactly.
"""
from __future__ import annotations

import sys
import types

import pytest

from aiforge_core.llm import user_agent as ua


@pytest.fixture
def _stub_ragas(monkeypatch):
    """Minimal stand-ins that record the kwargs they were constructed with."""
    seen: dict = {}

    class _Recorder:
        def __init__(self, **kw):
            seen[type(self).__name__] = kw

    class ChatOpenAI(_Recorder):
        pass

    class OpenAIEmbeddings(_Recorder):
        pass

    lco = types.ModuleType("langchain_openai")
    lco.ChatOpenAI = ChatOpenAI
    lco.OpenAIEmbeddings = OpenAIEmbeddings

    ragas = types.ModuleType("ragas")
    ragas.evaluate = lambda *a, **k: {}
    metrics = types.ModuleType("ragas.metrics")
    for name in ("answer_relevancy", "context_precision", "faithfulness"):
        setattr(metrics, name, object())

    datasets = types.ModuleType("datasets")
    datasets.Dataset = types.SimpleNamespace(from_dict=lambda d: d)

    monkeypatch.setitem(sys.modules, "langchain_openai", lco)
    monkeypatch.setitem(sys.modules, "ragas", ragas)
    monkeypatch.setitem(sys.modules, "ragas.metrics", metrics)
    monkeypatch.setitem(sys.modules, "datasets", datasets)
    return seen


def _run(monkeypatch):
    from aiforge_core.integrations import ragas_adapter
    monkeypatch.setattr(ragas_adapter, "_average_scores", lambda r: {})
    return ragas_adapter.evaluate_recall(
        [{"question": "q", "contexts": ["c"], "answer": "a"}],
        base_url="http://x/v1", api_key="k", model="m")


def test_the_judge_is_attributed(_stub_ragas, monkeypatch):
    _run(monkeypatch)
    assert _stub_ragas["ChatOpenAI"]["default_headers"] == {
        "User-Agent": ua.user_agent()}


def test_the_embedder_is_attributed_too(_stub_ragas, monkeypatch):
    """Easy to fix the judge and forget this one — it sends per CHUNK, so it is
    the higher-volume half of the pair."""
    _run(monkeypatch)
    assert _stub_ragas["OpenAIEmbeddings"]["default_headers"] == {
        "User-Agent": ua.user_agent()}


def test_neither_is_left_on_the_sdk_default(_stub_ragas, monkeypatch):
    _run(monkeypatch)
    for cls in ("ChatOpenAI", "OpenAIEmbeddings"):
        sent = _stub_ragas[cls]["default_headers"]["User-Agent"]
        assert "OpenAI/Python" not in sent, cls


def test_the_embedder_can_point_somewhere_else(_stub_ragas, monkeypatch):
    """A separate embedding endpoint is the documented setup, and it must still
    be attributed — a header wired only on the shared path would look right
    until someone split the two."""
    from aiforge_core.integrations import ragas_adapter
    monkeypatch.setattr(ragas_adapter, "_average_scores", lambda r: {})

    ragas_adapter.evaluate_recall(
        [{"question": "q", "contexts": ["c"], "answer": "a"}],
        base_url="http://x/v1", api_key="k", model="m",
        embed_base_url="http://embeddings/v1", embed_model="bge")

    emb = _stub_ragas["OpenAIEmbeddings"]
    assert emb["base_url"] == "http://embeddings/v1"
    assert emb["default_headers"] == {"User-Agent": ua.user_agent()}


# ── the adapter's own helpers ────────────────────────────────────────────

def test_availability_is_a_question_not_an_exception(monkeypatch):
    """Callers branch on this to decide whether to offer RAG eval at all."""
    from aiforge_core.integrations import ragas_adapter
    monkeypatch.setitem(sys.modules, "ragas", None)
    assert ragas_adapter.available() is False


def test_availability_is_true_when_all_three_import(_stub_ragas):
    from aiforge_core.integrations import ragas_adapter
    assert ragas_adapter.available() is True


def test_scores_are_averaged_per_metric():
    from aiforge_core.integrations import ragas_adapter

    class _Result:
        def to_pandas(self):
            import types as _t
            cols = {"question": ["q", "q"], "faithfulness": [1.0, 0.5]}

            class _DF:
                columns = list(cols)

                def __getitem__(self, k):
                    return _t.SimpleNamespace(tolist=lambda: cols[k])
            return _DF()

    assert ragas_adapter._average_scores(_Result()) == {"faithfulness": 0.75}


def test_an_unusable_score_table_falls_back_to_the_raw_repr():
    """A shape change in ragas must degrade the report, not raise through the
    caller — the numbers are the point, but losing them is not a crash."""
    from aiforge_core.integrations import ragas_adapter

    class _Broken:
        scores = [{"faithfulness": 1.0}]

        def to_pandas(self):
            raise RuntimeError("ragas changed shape")

    out = ragas_adapter._average_scores(_Broken())
    assert "raw" in out


def test_ground_truth_is_only_sent_when_every_sample_has_one(_stub_ragas,
                                                             monkeypatch):
    """context_precision needs it; a partial column would score some samples
    against a blank truth and quietly drag the average down."""
    from aiforge_core.integrations import ragas_adapter
    monkeypatch.setattr(ragas_adapter, "_average_scores", lambda r: {})

    rows = ragas_adapter._ragas_dataset(
        [{"question": "q", "contexts": ["c"], "answer": "a",
          "ground_truth": "g"}], True)
    assert "ground_truth" in rows

    rows = ragas_adapter._ragas_dataset(
        [{"question": "q", "contexts": ["c"], "answer": "a"}], False)
    assert "ground_truth" not in rows
