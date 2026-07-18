"""Intra-repo analysis planning: a single-repo doc/analysis task naming MANY
files is split into bounded read-only groups (discover → batch-read → synthesize)
so a local model never faces a flat multi-file sweep it can't track."""
from __future__ import annotations

from aiforge_core.runtime import analysis_pipeline as ap


def _mkfiles(root, names):
    for n in names:
        p = root / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"class {p.stem} {{}}")


def test_discover_keeps_only_real_files_dropping_typos(tmp_path):
    _mkfiles(tmp_path, ["pkg/A.java", "pkg/B.java"])
    prompt = ("summarise pkg/A.java, pkg/B.java, pkg/Ghost.java and "
              "onesell/Typo.java")   # last two do not exist
    found = ap._discover_target_files(prompt, str(tmp_path))
    assert set(found) == {"pkg/A.java", "pkg/B.java"}   # non-existent dropped


def test_plan_groups_many_files_and_skips_few(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_ANALYSIS_MIN_FILES", "6")
    monkeypatch.setenv("AIFORGE_ANALYSIS_FILES_PER_GROUP", "4")
    names = [f"m{i}.java" for i in range(9)]
    _mkfiles(tmp_path, names)
    prompt = "read " + ", ".join(names) + " and summarise each"
    plan, groups, _topics = ap.plan_single_repo(prompt, str(tmp_path))
    assert plan is True
    assert len(groups) == 3                     # 9 files / 4 per group → 4+4+1
    assert [len(g["files"]) for g in groups] == [4, 4, 1]
    assert all(g["path"] == str(tmp_path) for g in groups)

    # fewer than the minimum → no plan (single agent handles it)
    prompt2 = "look at m0.java and m1.java"
    plan2, groups2, _ = ap.plan_single_repo(prompt2, str(tmp_path))
    assert plan2 is False and groups2 == []


def test_stream_planned_fans_out_and_synthesizes(tmp_path, monkeypatch):
    names = [f"m{i}.java" for i in range(6)]
    _mkfiles(tmp_path, names)
    prompt = "summarise " + ", ".join(names)

    # stub the per-group explore + synthesis so no LLM is needed
    seen_groups = []

    def fake_explore(group, topics, overall):
        seen_groups.append(tuple(group["files"]))
        return {"name": group["name"], "path": group["path"], "ok": True,
                "findings": f"findings for {group['name']}"}

    def fake_synth(overall, results, topics):
        return "DRAFT(" + "|".join(r["name"] for r in results) + ")"

    monkeypatch.setattr(ap, "_explore_files_group", fake_explore)
    monkeypatch.setattr(ap, "_synthesize", fake_synth)
    monkeypatch.setenv("AIFORGE_ANALYSIS_FILES_PER_GROUP", "3")

    events = list(ap.stream_analysis_planned(prompt, cwd=str(tmp_path)))
    types = [e["type"] for e in events]
    assert "subtasks" in types
    # 6 files / 3 per group → 2 groups → 2 explore agents, both done
    assert len(seen_groups) == 2
    done = [e for e in events if e["type"] == "subtask_update"
            and e["status"] == "done"]
    assert len(done) == 2
    final = [e for e in events if e["type"] == "message"][-1]
    assert final["text"].startswith("DRAFT(")
    assert events[-1]["type"] == "done"


def test_stream_planned_handles_no_files_gracefully(tmp_path):
    # nothing real referenced → empty groups → clean "no file group" message
    events = list(ap.stream_analysis_planned("summarise ghost.java",
                                             cwd=str(tmp_path), groups=[], topics=[]))
    assert events[-1]["type"] == "done"
    assert any(e["type"] == "message" for e in events)
