from aiforge_core.context import assemble_prompt, PromptInputs
from aiforge_core.retrieval import Hit


def _hit(id_, text, tier="t4", source=None):
    return Hit(id=id_, score=1.0, tier=tier, text=text, source=source or id_)


def test_assemble_prompt_never_compresses_code_or_task():
    big_code = "def foo(): pass  # " + ("x" * 10_000)
    inputs = PromptInputs(
        role="developer",
        system_prompt="You are Developer.",
        task_body="Fix the bug in foo.",
        retrieved_code=[_hit("code:a.py#foo", big_code)],
        retrieved_memory=[],
        prior_hops=[],
        tool_schemas=[],
        output_contract="return JSON",
    )
    out = assemble_prompt(inputs, budget_bytes=100_000)
    assert big_code in out
    assert "Fix the bug in foo." in out


def test_assemble_prompt_drops_lowest_ranked_memory_first_when_over_budget():
    inputs = PromptInputs(
        role="developer",
        system_prompt="sys",
        task_body="task",
        retrieved_code=[],
        retrieved_memory=[_hit(f"mem:{i}", "m" * 2000, tier="t1") for i in range(20)],
        prior_hops=[],
        tool_schemas=[],
        output_contract="",
    )
    out = assemble_prompt(inputs, budget_bytes=6000)
    # Should only fit ~2 memory blocks
    assert out.count("m" * 2000) < 20
    assert "sys" in out
    assert "task" in out


from aiforge_core.context import compact_hop


def test_compact_hop_summary_is_bulleted_under_cap(monkeypatch):
    monkeypatch.setattr(
        "aiforge_core.context._llm_summarize",
        lambda text, cap: "- did X\n- got Y\n- next Z",
    )
    raw = "x" * 10_000
    summary = compact_hop(role="developer", raw_text=raw, cap_chars=200)
    assert summary.startswith("- ")
    assert len(summary) < 200


from aiforge_core.context import GraphInsight


def test_assemble_prompt_includes_graph_insights_section():
    inputs = PromptInputs(
        role="architect",
        system_prompt="sys",
        task_body="task",
        retrieved_code=[],
        retrieved_memory=[],
        prior_hops=[],
        tool_schemas=[],
        output_contract="",
        graph_insights=[GraphInsight(title="Cluster: sync", text="NATS + JetStream tie push flow")],
    )
    out = assemble_prompt(inputs, budget_bytes=100_000)
    assert "GRAPH INSIGHTS" in out
    assert "Cluster: sync" in out
