"""Wire-to-wire integration smoke: every layer in one test run.

All layers participate:
- embed (mocked urlopen → 1024-d vector)
- retrieval (FakeStore returns hand-crafted Hits per tier)
- rerank_http (monkeypatched to return input unchanged)
- context.assemble_prompt (asserts role sections + compaction rules)
- reflection.parse_reflection_xml + submit_proposals (FakeStore captures writes)
- permissions + role-dir sanity check
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock
import json

import yaml
import pytest

from aiforge_core import embed as embed_mod
from aiforge_core.retrieval import Hit, retrieve_for_role, ROLE_POLICIES
from aiforge_core.context import (
    PromptInputs, PriorHop, GraphInsight, assemble_prompt,
)
from aiforge_core.reflection import (
    parse_reflection_xml, submit_proposals, ReflectionResult, Fact, Recipe,
)
from aiforge_core.config import PaperclipConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------- fakes ----------
class FakeStore:
    """In-memory stand-in for store_v2.Store. Captures propose calls."""

    def __init__(self):
        self.tier_bm25_calls = []
        self.tier_vec_calls = []
        self.proposals: list[dict] = []

    def search_tier_bm25(self, tier, query, top_k, wing_prefix=None):
        self.tier_bm25_calls.append((tier, top_k))
        return [Hit(id=f"mem:{tier}-bm-{i}", score=0.1, tier=tier,
                    text=f"bm25 hit {i} about {query[:30]}", source=f"{tier}/bm/{i}")
                for i in range(min(3, top_k))]

    def search_tier_vec(self, tier, query, top_k, wing_prefix=None):
        self.tier_vec_calls.append((tier, top_k))
        return [Hit(id=f"mem:{tier}-vec-{i}", score=0.1, tier=tier,
                    text=f"vec hit {i} about {query[:30]}", source=f"{tier}/vec/{i}")
                for i in range(min(3, top_k))]

    def propose(self, *, tier, wing, kind, text, source_trace, proposed_by,
                title=None, metadata=None):
        self.proposals.append({
            "tier": tier, "wing": wing, "kind": kind, "text": text,
            "source_trace": source_trace, "proposed_by": proposed_by,
            "title": title, "metadata": metadata,
        })
        return len(self.proposals)


def _fake_urlopen(response_json):
    m = MagicMock()
    m.__enter__.return_value = m
    m.read.return_value = json.dumps(response_json).encode()
    return m


# ---------- layer tests ----------

def test_embed_layer_mocked():
    """embed() talks to the sidecar via urlopen — mock round-trip."""
    with patch.object(embed_mod.urllib.request, "urlopen",
                      return_value=_fake_urlopen({"embedding": [0.5] * 1024})):
        v = embed_mod.embed("integration probe")
    assert len(v) == 1024


def test_retrieval_pipeline_per_role():
    """retrieve_for_role drives BM25 + vec + RRF + rerank end-to-end."""
    store = FakeStore()
    with patch("aiforge_core.retrieval.rerank_http",
               side_effect=lambda q, hits, keep: hits[:keep]):
        out = retrieve_for_role(store, role="developer", query="sync flow", parent_id="TICKET-42")

    policy = ROLE_POLICIES["developer"]
    queried_tiers = [c[0] for c in store.tier_bm25_calls]
    assert queried_tiers == [t["tier"] for t in policy["tiers"]]
    assert len(out) <= policy["rerank_keep"]


def test_context_assembly_with_graph_and_memory():
    """context.assemble_prompt packs system/task/code/memory/graph sections."""
    code_hit = Hit(id="code:a.py#foo", score=0.9, tier="t4",
                   text="def foo(): return 42", source="a.py#foo")
    mem_hits = [Hit(id=f"mem:{i}", score=1.0 - i * 0.1, tier="t2",
                    text=f"fact {i}", source=f"t2/{i}") for i in range(3)]

    inputs = PromptInputs(
        role="architect",
        system_prompt="You are Architect.",
        task_body="Design the sync flow.",
        retrieved_code=[code_hit],
        retrieved_memory=mem_hits,
        prior_hops=[PriorHop(role="human", summary="opened ticket")],
        tool_schemas=[{"name": "search_memory"}],
        output_contract="return JSON",
        graph_insights=[GraphInsight(title="Cluster: sync", text="NATS ties push")],
    )
    prompt = assemble_prompt(inputs, budget_bytes=50_000)

    # All top-level sections present
    assert "SYSTEM" in prompt
    assert "TASK" in prompt
    assert "RETRIEVED CODE (do not compress)" in prompt
    assert "GRAPH INSIGHTS" in prompt
    assert "Cluster: sync" in prompt
    assert "OUTPUT CONTRACT" in prompt
    assert "TOOLS" in prompt
    assert "RECENT WORK (compacted)" in prompt
    # Code chunk preserved verbatim
    assert "def foo(): return 42" in prompt


def test_reflection_xml_to_proposals_to_store():
    """Full reflection loop: XML → ReflectionResult → memory_proposals."""
    store = FakeStore()
    xml = """<reflection>
      <facts>
        <fact kind="convention">Use pgvector HNSW for cosine.</fact>
      </facts>
      <recipes>
        <recipe title="T4 reindex">
          <when>After push to main.</when>
          <how>Run make graphify-rebuild then aiforge memory reindex-code.</how>
        </recipe>
      </recipes>
    </reflection>"""
    result = parse_reflection_xml(xml)
    assert isinstance(result, ReflectionResult)
    ids = submit_proposals(store, parent_id="TICKET-42", result=result)

    assert len(ids) == 2  # 1 fact + 1 recipe
    tiers = {p["tier"] for p in store.proposals}
    assert tiers == {"t2", "t3"}
    # Recipe carries WHEN/HOW structure
    recipe = next(p for p in store.proposals if p["tier"] == "t3")
    assert "WHEN:" in recipe["text"] and "HOW:" in recipe["text"]


def test_four_roles_have_required_permissions():
    """Every v4.1 role exposes search_memory and report. Only developer writes."""
    for role in ("architect", "sr-developer", "developer", "fact-extract"):
        p = REPO_ROOT / "agents" / role / "permissions.yml"
        assert p.exists(), f"permissions.yml missing for {role}"
        doc = yaml.safe_load(p.read_text())
        can = doc.get("can") or {}
        assert can.get("search_memory") is True, f"{role} missing search_memory"
        assert can.get("report") is True, f"{role} missing report"
        if role == "developer":
            assert can.get("write_file") is True
            assert can.get("git_ops") is True
        else:
            assert can.get("write_file") is False, f"{role} must NOT write files"


def test_config_loads_v41_shape():
    """PaperclipConfig v4.1 has all required dataclass fields."""
    cfg = PaperclipConfig.load(REPO_ROOT)
    assert set(cfg.budgets.keys()) == {"architect", "sr_developer", "developer", "fact_extract"}
    assert cfg.confidence.proceed_threshold == pytest.approx(0.7)
    assert cfg.kill_switch.global_file == ".aiforge/KILL"
    assert cfg.routing.initial_assignee_parent == "architect"
    assert cfg.routing.initial_assignee_child == "developer"
    assert cfg.architect_mode.mode in ("cloud", "local_30b")
