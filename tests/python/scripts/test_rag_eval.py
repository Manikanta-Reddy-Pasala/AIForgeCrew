"""scripts/rag_eval.py — scoring the memory RAG pipeline with ragas.

ragas is a dev-tool OVERLAY, not a project extra (its langchain pins conflict
with the app's own), so the first thing this script does is check whether it is
importable and, when it is not, print the exact `uv run --with` line rather
than a traceback. That's the branch most people hit.

The rest is a straight pipeline — recall contexts, answer from ONLY those
contexts, judge with the same local endpoint — and the tests pin the shape of
each hop with everything stubbed: no model, no memory, no ragas.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[3] / "scripts" / "rag_eval.py"


@pytest.fixture
def re_mod():
    spec = importlib.util.spec_from_file_location("rag_eval_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rag_eval_under_test"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("rag_eval_under_test", None)


def _args(**kw):
    base = {"questions": None, "limit": 0, "repo": None, "role": "chat"}
    base.update(kw)
    return types.SimpleNamespace(**base)


# ─── the question set ──────────────────────────────────────────────────


def test_the_builtin_smoke_set_is_used_without_a_file(re_mod):
    rows = re_mod._samples(_args())
    assert len(rows) == 3
    assert all("question" in r for r in rows)


def test_the_builtin_set_honours_a_limit(re_mod):
    assert len(re_mod._samples(_args(limit=2))) == 2


def test_questions_are_read_one_json_object_per_line(re_mod, tmp_path):
    f = tmp_path / "q.jsonl"
    f.write_text(json.dumps({"question": "a", "ground_truth": "A"}) + "\n"
                 + "\n"                                   # blank line skipped
                 + json.dumps({"question": "b"}) + "\n")
    rows = re_mod._samples(_args(questions=str(f)))
    assert [r["question"] for r in rows] == ["a", "b"]
    assert rows[0]["ground_truth"] == "A"


def test_a_questions_file_honours_the_limit(re_mod, tmp_path):
    f = tmp_path / "q.jsonl"
    f.write_text("".join(json.dumps({"question": str(i)}) + "\n" for i in range(5)))
    assert len(re_mod._samples(_args(questions=str(f), limit=2))) == 2


# ─── the run ───────────────────────────────────────────────────────────


@pytest.fixture
def wired(re_mod, monkeypatch):
    """Stub ragas, the LLM and memory; capture what each hop received."""
    from aiforge_core.integrations import ragas_adapter
    from aiforge_core.llm import client
    from aiforge_core.memory import unified_query
    state: dict = {"available": True, "hits": [{"text": "context one"}],
                   "answer": "the answer", "scores": {"faithfulness": 0.9},
                   "queries": [], "completions": [], "evaluated": None}

    monkeypatch.setattr(ragas_adapter, "available", lambda: state["available"])

    def _evaluate(samples, base_url=None, api_key=None, model=None):
        state["evaluated"] = {"samples": samples, "base_url": base_url,
                              "model": model}
        return state["scores"]
    monkeypatch.setattr(ragas_adapter, "evaluate_recall", _evaluate)

    class _EP:
        base_url = "http://box:1234/v1"
        api_key = "sk-x"
        model = "qwen"
    monkeypatch.setattr(client, "resolve", lambda role: _EP())

    def _complete(role, messages, max_tokens=None):
        state["completions"].append({"role": role, "messages": messages})
        return state["answer"]
    monkeypatch.setattr(client, "complete", _complete)

    def _query(q, limit=None, repo=None):
        state["queries"].append({"q": q, "limit": limit, "repo": repo})
        return {"hits": state["hits"]}
    monkeypatch.setattr(unified_query, "query", _query)
    monkeypatch.setattr(sys, "argv", ["rag_eval.py", "--limit", "1"])
    return state


def test_a_missing_ragas_prints_the_overlay_command(re_mod, wired, capsys):
    """The langchain pins conflict, so it can never be a plain project extra."""
    wired["available"] = False
    assert re_mod.main() == 2
    err = capsys.readouterr().err
    assert "uv run --with 'ragas<0.4'" in err


def test_each_question_is_recalled_then_answered_from_its_contexts(re_mod, wired,
                                                                   capsys):
    assert re_mod.main() == 0
    assert len(wired["queries"]) == 1
    assert wired["queries"][0]["limit"] == 6
    user = wired["completions"][0]["messages"][1]["content"]
    assert "context one" in user
    assert "Question:" in user
    system = wired["completions"][0]["messages"][0]["content"]
    assert "ONLY from the provided context" in system


def test_the_repo_scopes_the_recall(re_mod, wired, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["rag_eval.py", "--repo", "AIForgeCrew",
                                      "--limit", "1"])
    re_mod.main()
    assert wired["queries"][0]["repo"] == "AIForgeCrew"


def test_empty_contexts_are_dropped(re_mod, wired):
    wired["hits"] = [{"text": "real"}, {"text": "   "}, {}, None]
    re_mod.main()
    assert wired["evaluated"]["samples"][0]["contexts"] == ["real"]


def test_the_context_list_is_capped(re_mod, wired):
    wired["hits"] = [{"text": f"c{i}"} for i in range(20)]
    re_mod.main()
    assert len(wired["evaluated"]["samples"][0]["contexts"]) == 6


def test_a_ground_truth_rides_into_the_sample(re_mod, wired, tmp_path, monkeypatch):
    f = tmp_path / "q.jsonl"
    f.write_text(json.dumps({"question": "q", "ground_truth": "A"}) + "\n")
    monkeypatch.setattr(sys, "argv", ["rag_eval.py", "--questions", str(f)])
    re_mod.main()
    assert wired["evaluated"]["samples"][0]["ground_truth"] == "A"


def test_a_question_without_a_ground_truth_omits_the_key(re_mod, wired):
    re_mod.main()
    assert "ground_truth" not in wired["evaluated"]["samples"][0]


def test_the_judge_uses_the_same_endpoint_as_the_answerer(re_mod, wired):
    re_mod.main()
    assert wired["evaluated"]["base_url"] == "http://box:1234/v1"
    assert wired["evaluated"]["model"] == "qwen"


def test_an_empty_answer_is_still_a_sample(re_mod, wired):
    wired["answer"] = None
    re_mod.main()
    assert wired["evaluated"]["samples"][0]["answer"] == ""


def test_the_scores_are_printed_with_the_judge(re_mod, wired, capsys):
    wired["scores"] = {"faithfulness": 0.91, "answer_relevancy": 0.8}
    re_mod.main()
    out = capsys.readouterr().out
    assert "judge=qwen" in out
    assert "faithfulness" in out
    assert "0.91" in out
    assert "recalled 1 contexts for:" in out
