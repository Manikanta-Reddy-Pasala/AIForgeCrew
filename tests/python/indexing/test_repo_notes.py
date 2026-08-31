"""repo_notes: build a REPO_NOTES.md by scanning a repo with ripgrep + regex.

Fixtures write a small synthetic Java/Python repo to tmp_path and let the real
extractors run over it, so the regexes are exercised against real text rather
than asserted about.
"""
from __future__ import annotations

import os
import shutil

import pytest

from aiforge_core.indexing import repo_notes as rn

pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None,
    reason="repo_notes scans with ripgrep; without it every extractor is empty")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A synthetic repo under AIFORGE_REPOS_BASE, as generate_repo_notes expects."""
    base = tmp_path / "repos"
    wt = base / "demo"
    (wt / "src").mkdir(parents=True)
    monkeypatch.setenv("AIFORGE_REPOS_BASE", str(base))
    return wt


def _java(repo, name: str, body: str):
    p = repo / "src" / name
    p.write_text(body, encoding="utf-8")
    return p


# ── README lead ───────────────────────────────────────────────────────


def test_readme_lead_takes_the_paragraph_after_the_h1():
    txt = "# Title\n\nThe purpose line.\nSecond line.\n\nLater section."
    assert rn._readme_lead(txt) == "The purpose line. Second line."


def test_readme_lead_skips_padding_before_the_paragraph():
    assert rn._readme_lead("\n\n# T\n\n## Sub\n\nReal text.") == "Real text."


def test_readme_lead_of_an_empty_readme_is_empty():
    assert rn._readme_lead("") == ""


def test_readme_lead_is_length_capped():
    assert len(rn._readme_lead("# T\n\n" + ("word " * 500))) <= 1200


def test_sniff_purpose_reports_when_there_is_no_readme(repo):
    assert rn._sniff_purpose(str(repo)) == "(no README found)"


def test_sniff_purpose_reads_the_readme(repo):
    (repo / "README.md").write_text("# Demo\n\nA demo service.\n")
    assert rn._sniff_purpose(str(repo)) == "A demo service."


# ── layout ────────────────────────────────────────────────────────────


def test_layout_counts_files_per_top_level_dir(repo):
    (repo / "src" / "a.java").write_text("x")
    (repo / "src" / "b.java").write_text("x")
    (repo / "docs").mkdir()
    (repo / "docs" / "d.md").write_text("x")
    rows = rn._layout(str(repo))
    assert any("src/" in r and "(2 files)" in r for r in rows)
    assert any("docs/" in r and "(1 files)" in r for r in rows)


def test_layout_skips_dotdirs(repo):
    (repo / ".git").mkdir()
    (repo / ".git" / "x").write_text("x")
    assert not any(".git" in r for r in rn._layout(str(repo)))


def test_layout_of_a_missing_path_is_empty():
    assert rn._layout("/definitely/not/here") == []


# ── controller endpoints ──────────────────────────────────────────────


def test_controller_endpoints_join_class_and_method_paths():
    content = '@GetMapping("/list")\n@PostMapping(value = "/create")\n'
    out = rn._controller_endpoints(content, "/api")
    assert "GET /api/list" in out
    assert "POST /api/create" in out


def test_controller_endpoints_collapse_a_double_slash():
    out = rn._controller_endpoints('@GetMapping("/x")', "/api/")
    assert out == ["GET /api/x"]


def test_controller_endpoints_of_a_file_with_none():
    assert rn._controller_endpoints("class Foo {}", "/api") == []


def test_controllers_finds_a_rest_controller(repo):
    _java(repo, "UserController.java",
          '@RestController\n@RequestMapping("/api/users")\n'
          'class UserController {\n  @GetMapping("/{id}")\n  void get() {}\n}\n')
    found = rn._controllers(str(repo))
    assert found, "the @RestController must be picked up"
    assert any("users" in str(c) for c in found)


# ── other extractors ──────────────────────────────────────────────────


def test_configs_lists_configuration_classes(repo):
    _java(repo, "AppConfig.java", "@Configuration\nclass AppConfig {}\n")
    assert any("AppConfig.java" in c for c in rn._configs(str(repo)))


def test_nats_subjects_split_publish_from_subscribe(repo):
    _java(repo, "Sync.java",
          'natsClient.publish("business.push.request", body);\n'
          '@NatsListener(subject = "client.pull.response")\n')
    out = rn._nats_subjects(str(repo))
    assert "business.push.request" in out["publish"]
    assert "client.pull.response" in out["subscribe"]


def test_nats_subjects_ignores_short_non_dotted_strings(repo):
    _java(repo, "S.java", 'natsClient.publish("hi", body);\n')
    out = rn._nats_subjects(str(repo))
    assert out["publish"] == [], "a bare word is not a subject"


def test_mongo_collections_are_extracted(repo):
    _java(repo, "Repo.java",
          '@Document(collection = "productTxn")\nclass P {}\n')
    cols = rn._mongo_collections(str(repo))
    assert isinstance(cols, list)


# ── the public entry point ────────────────────────────────────────────


def test_generate_repo_notes_raises_for_an_unknown_repo(repo):
    with pytest.raises(FileNotFoundError):
        rn.generate_repo_notes("no-such-repo")


def test_generate_repo_notes_renders_without_writing(repo):
    (repo / "README.md").write_text("# Demo\n\nA demo service.\n")
    _java(repo, "UserController.java",
          '@RestController\n@RequestMapping("/api/users")\nclass U {}\n')
    body = rn.generate_repo_notes("demo", write=False)
    assert "A demo service." in body
    assert not (repo / ".aiforge" / "REPO_NOTES.md").exists(), \
        "write=False must not touch the disk"


def test_generate_repo_notes_writes_the_file_and_returns_its_path(repo):
    (repo / "README.md").write_text("# Demo\n\nA demo service.\n")
    path = rn.generate_repo_notes("demo")
    assert os.path.isfile(path)
    assert path.endswith("REPO_NOTES.md")
    assert "A demo service." in open(path, encoding="utf-8").read()
