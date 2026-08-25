"""ragas adapter — RAG recall quality metrics.

ragas is a dev-tool OVERLAY, not a project extra (its langchain pins conflict
with aider-chat in one resolution universe): run consumers via
``uv run --with 'ragas<0.4' --with 'langchain-openai<1' …``.

One capability: :func:`evaluate_recall` scores (question, contexts, answer)
samples with LLM-judged metrics (faithfulness, answer relevancy, context
precision), using OUR configured role endpoint as the judge — fully local,
no cloud. Raises on any failure — the caller (scripts/rag_eval.py) reports
and exits; there is no silent fallback for an eval.
"""
from __future__ import annotations


def available() -> bool:
    try:
        import ragas            # noqa: F401
        import datasets         # noqa: F401
        import langchain_openai  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _ragas_dataset(samples: list[dict], has_gt: bool):
    """Build the ragas Dataset from the samples (adds ``ground_truth`` only when
    every sample carries it)."""
    from datasets import Dataset
    data = {
        "question": [s["question"] for s in samples],
        "contexts": [list(s.get("contexts") or []) for s in samples],
        "answer": [s.get("answer") or "" for s in samples],
    }
    if has_gt:
        data["ground_truth"] = [s["ground_truth"] for s in samples]
    return Dataset.from_dict(data)


def _average_scores(result) -> dict:
    """Average the per-sample ragas score table per metric. Falls back to the
    raw repr on any error."""
    try:
        df = result.to_pandas()
        out: dict = {}
        for m in df.columns:
            if m in ("question", "contexts", "answer", "ground_truth"):
                continue
            vals = [v for v in df[m].tolist() if isinstance(v, (int, float))]
            if vals:
                out[m] = round(sum(vals) / len(vals), 4)
        return out
    except Exception:  # noqa: BLE001 — fall back to the raw repr
        return {"raw": str(getattr(result, "scores", None) or result)}


def evaluate_recall(samples: list[dict], *, base_url: str, api_key: str,
                    model: str, embed_base_url: str | None = None,
                    embed_model: str | None = None) -> dict:
    """Score RAG samples. Each sample: ``{question, contexts: [str], answer}``
    (optional ``ground_truth``). Returns ``{metric_name: score}`` averages.

    Judge + embeddings run against the given OpenAI-compatible endpoint(s) —
    point these at the local LM Studio / office cluster."""
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from ragas import evaluate
    from ragas.metrics import (answer_relevancy, context_precision,
                               faithfulness)

    judge = ChatOpenAI(base_url=base_url, api_key=api_key or "not-needed",
                       model=model, temperature=0)
    embeddings = OpenAIEmbeddings(
        base_url=embed_base_url or base_url, api_key=api_key or "not-needed",
        model=embed_model or "text-embedding-bge-m3",
        check_embedding_ctx_length=False)

    has_gt = all(s.get("ground_truth") for s in samples)
    metrics = [faithfulness, answer_relevancy]
    if has_gt:
        metrics.append(context_precision)
    result = evaluate(_ragas_dataset(samples, has_gt), metrics=metrics,
                      llm=judge, embeddings=embeddings)
    return _average_scores(result)


__all__ = ["available", "evaluate_recall"]
