"""The on-disk layout rule, in one place.

OKF ids are per-scope counters, so (nuc, O-01) and (ms, O-01) are different
objects that both render to O-01.md. These functions are the only thing that
decides where an identity lives.
"""
from __future__ import annotations


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))


def _node(tmp_path, scope: str, origin: str, key: str):
    p = tmp_path / "md" / "okf" / scope / "learnings" / f"{key}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'---\ntype: learning\nid: "{key}"\norigin: "{origin}"\n'
                 f'rev: 1\nupdated_by: "{origin}"\n---\n\nb\n', encoding="utf-8")
    return p


def test_node_paths_matches_on_origin_not_just_filename(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    mine = _node(tmp_path, "global", "book", "O-01")
    _node(tmp_path, "peers/ms", "ms", "O-01")

    assert paths.node_paths("book", "O-01") == [mine]


def test_node_paths_is_empty_for_an_unknown_identity(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    assert paths.node_paths("nuc", "L-99") == []


def test_tomb_and_lease_paths_are_stable(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    assert paths.tomb_path("nuc", "L-07").as_posix().endswith(
        "okf/.tomb/nuc/L-07.json")
    assert paths.lease_path().as_posix().endswith("okf/.lease.json")


def test_target_for_known_identity_is_updated_in_place(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    mine = _node(tmp_path, "global", "nuc", "L-07")
    entry = {"cls": "B", "origin": "nuc", "key": "L-07",
             "path": "okf/peers/nuc/L-07.md"}   # sender's layout differs

    assert paths.target_for(entry) == mine


def test_target_for_a_new_foreign_node_lands_under_peers(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    entry = {"cls": "B", "origin": "ms", "key": "O-01",
             "path": "okf/global/objectives/O-01.md"}

    assert paths.target_for(entry).as_posix().endswith("okf/peers/ms/O-01.md")


def test_target_for_class_a_uses_the_advertised_path(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    entry = {"cls": "A", "path": "captures/a.md"}

    assert paths.target_for(entry).as_posix().endswith("captures/a.md")


def test_target_for_class_a_still_refuses_to_escape(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    assert paths.target_for({"cls": "A", "path": "../../evil"}) is None


def test_a_hostile_origin_or_key_cannot_climb_the_tree(monkeypatch, tmp_path):
    """origin and key come from a peer's frontmatter — treat them as attacker input."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    root = (tmp_path / "md").resolve()

    for origin, key in (("../../..", "L-07"),
                        ("nuc", "../../../../etc/passwd"),
                        ("..", ".."),
                        ("", "")):
        for p in (paths.peer_node_path(origin, key), paths.tomb_path(origin, key)):
            assert root in p.resolve().parents


def test_target_for_a_tombstone_and_the_lease(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import paths

    tomb = paths.target_for({"cls": "B", "origin": "nuc", "key": "L-07",
                             "tomb": True, "path": "x"})
    lease = paths.target_for({"cls": "B", "origin": "", "key": "__lease__",
                              "path": "x"})

    assert tomb.as_posix().endswith("okf/.tomb/nuc/L-07.json")
    assert lease.as_posix().endswith("okf/.lease.json")
