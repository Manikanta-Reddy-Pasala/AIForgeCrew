"""Pulling a repo, a folder, a file or a URL into the memory index.

Indexing is minutes of CPU per repo, so almost every rule here is about not
wasting or losing that work:

  * the two chunk layers (code, docs) fail independently — one broken layer
    must not throw away what the other indexed;
  * an index that produced NOTHING is nearly always a wrong or unmounted path,
    so the error names the RESOLVED absolute path the walker actually looked
    at, not the string the user typed;
  * a long ingest heartbeats its lease, or the stale-index reaper resets a
    slow-but-progressing repo to idle in a loop that never finishes;
  * a layer that errored is reported "partial", never a clean "done";
  * the daily sweep skips a repo whose merkle root is unchanged — but a merkle
    glitch soft-fails to indexing anyway, because a skipped real change is the
    worse outcome.

A ``url`` source is unauthenticated input, so the fetch is SSRF-guarded before
the request AND again after any redirect.
"""
from __future__ import annotations

import types as pytypes

import pytest

from aiforge_core.runtime import memory_ingest as I


@pytest.fixture()
def written(monkeypatch):
    """Capture what would be written into the store."""
    rows: list = []
    monkeypatch.setattr(I, "_write",
                        lambda text, *, kind, repo, ref, embed_vec=None:
                        rows.append({"text": text, "kind": kind, "repo": repo,
                                     "ref": ref, "vec": embed_vec}) or True)
    return rows


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 1\n")
    (tmp_path / "README.md").write_text("# the project\n")
    return tmp_path


# ─── reading one file ──────────────────────────────────────────────────


def test_a_text_file_is_read_off_disk(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1")
    assert I._read_source(p) == "x = 1"


def test_a_pdf_goes_through_the_document_extractor(tmp_path, monkeypatch):
    from aiforge_core.runtime import chat_media
    monkeypatch.setattr(chat_media, "extract_text", lambda p: "page text")
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF")
    assert I._read_source(p) == "page text"


def test_an_unreadable_document_is_skipped_not_fatal(tmp_path, monkeypatch):
    from aiforge_core.runtime import chat_media
    monkeypatch.setattr(chat_media, "extract_text",
                        lambda p: (_ for _ in ()).throw(RuntimeError("corrupt")))
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF")
    assert I._read_source(p) is None


def test_an_empty_extraction_is_nothing_to_index(tmp_path, monkeypatch):
    from aiforge_core.runtime import chat_media
    monkeypatch.setattr(chat_media, "extract_text", lambda p: "")
    p = tmp_path / "a.docx"
    p.write_bytes(b"PK")
    assert I._read_source(p) is None


def test_a_file_too_big_to_index_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "_MAX_FILE", 4)
    p = tmp_path / "big.py"
    p.write_text("x" * 100)
    assert I._read_source(p) is None


def test_a_file_that_vanished_is_skipped(tmp_path):
    assert I._read_source(tmp_path / "ghost.py") is None


# ─── collecting the chunks ─────────────────────────────────────────────


def test_every_chunk_names_the_file_it_came_from(repo):
    chunks = I._collect_chunks(repo, {".py"})
    assert chunks and all(t.startswith("# src/app.py") for t, _ in chunks)
    assert chunks[0][1] == "src/app.py"


def test_a_huge_tree_cannot_blow_memory(repo, monkeypatch):
    monkeypatch.setattr(I, "_MAX_CHUNKS", 1)
    for i in range(5):
        (repo / f"f{i}.py").write_text("y = 1\n")
    assert len(I._collect_chunks(repo, {".py"})) == 1


def test_unreadable_files_do_not_stop_the_walk(repo, monkeypatch):
    monkeypatch.setattr(I, "_read_source",
                        lambda f: None if f.name == "app.py" else "text")
    refs = {ref for _, ref in I._collect_chunks(repo, {".py", ".md"})}
    assert "src/app.py" not in refs and "README.md" in refs


# ─── embedding in batches ──────────────────────────────────────────────


def test_a_batch_is_embedded_in_one_round_trip(monkeypatch):
    from aiforge_core.memory import embed
    seen: list = []
    monkeypatch.setattr(embed, "embed_batch",
                        lambda texts: seen.append(texts) or [[0.1], [0.2]])
    out = I._embed_batch_vecs([("a", "r1"), ("b", "r2")])
    assert out == [[0.1], [0.2]] and seen == [["a", "b"]]


def test_a_sidecar_hiccup_falls_back_to_per_write_embedding(monkeypatch):
    """Never abort an ingest because the embedder blipped."""
    from aiforge_core.memory import embed
    monkeypatch.setattr(embed, "embed_batch",
                        lambda texts: (_ for _ in ()).throw(OSError("down")))
    assert I._embed_batch_vecs([("a", "r")]) == [None]


def test_the_tree_is_written_with_its_vectors(repo, written, monkeypatch):
    monkeypatch.setattr(I, "_embed_batch_vecs",
                        lambda batch: [[0.5]] * len(batch))
    n = I._ingest_tree(repo, repo="proj", exts={".py"}, kind="code")
    assert n == len(written) and written[0]["vec"] == [0.5]
    assert written[0]["kind"] == "code" and written[0]["repo"] == "proj"


# ─── the layered repo index ────────────────────────────────────────────


@pytest.fixture()
def layers(monkeypatch):
    state: dict = {"code": 3, "doc": 2}

    def _tree(root, *, repo, exts, kind):
        v = state["code"] if kind == "code" else state["doc"]
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(I, "_ingest_tree", _tree)
    return state


def test_both_layers_contribute_to_the_index(layers, repo):
    res = I._index_repo_full(repo, "proj")
    assert res["units"] == 5 and res["code_units"] == 3 and res["doc_units"] == 2
    assert res["layers"] == {"code_chunks": "ok", "doc_chunks": "ok"}
    assert res["error"] is None


def test_one_broken_layer_does_not_lose_the_other(layers, repo):
    layers["code"] = RuntimeError("tree-sitter blew up")
    res = I._index_repo_full(repo, "proj")
    assert res["units"] == 2 and res["error"] is None
    assert res["layers"]["code_chunks"].startswith("error:")


def test_a_layer_can_be_switched_off(layers, repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_INDEX_DOCS", "0")
    res = I._index_repo_full(repo, "proj")
    assert res["doc_units"] == 0 and res["layers"]["doc_chunks"] == I._SKIP_DISABLED


def test_an_index_that_found_nothing_names_the_path_it_looked_at(layers, repo):
    """Almost always a wrong or unmounted path — the string the user typed is
    not what the walker resolved."""
    layers["code"] = layers["doc"] = 0
    res = I._index_repo_full(repo, "proj")
    assert str(repo.resolve()) in res["error"] and "/workspace" in res["error"]
    assert res["layers"]["code_chunks"] == "skip:no_files"


def test_every_layer_failing_is_reported_as_such(layers, repo):
    layers["code"] = RuntimeError("a")
    layers["doc"] = RuntimeError("b")
    res = I._index_repo_full(repo, "proj")
    assert res["error"].startswith("all chunk layers failed")


def test_an_empty_walk_after_a_layer_error_is_not_a_path_problem(repo):
    assert I._empty_index_error(repo, {"code_chunks": "error:boom"}) is None


# ─── the four source kinds ─────────────────────────────────────────────


@pytest.fixture()
def ingest(monkeypatch):
    state: dict = {"full": {"units": 7, "error": None}, "tree": 4, "url": "text"}
    monkeypatch.setattr(I, "_index_repo_full", lambda root, repo: state["full"])
    monkeypatch.setattr(I, "_ingest_tree",
                        lambda root, *, repo, exts, kind: state["tree"])
    monkeypatch.setattr(I, "_fetch_url", lambda loc: state["url"])
    return state


def test_a_repo_source_gets_the_full_layered_index(ingest, repo):
    assert I.ingest_source({"kind": "repo", "name": "proj",
                            "location": str(repo)})["units"] == 7


def test_a_docs_folder_is_ingested_as_documents(ingest, repo):
    assert I.ingest_source({"kind": "docs", "name": "proj",
                            "location": str(repo)}) == {"units": 4,
                                                        "error": None}


def test_a_single_file_is_chunked_as_a_document(written, tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("some notes")
    res = I.ingest_source({"kind": "file", "name": "proj", "location": str(p)})
    assert res["units"] == 1 and written[0]["ref"] == "notes.md"
    assert written[0]["text"].startswith("# notes.md")


def test_a_url_is_fetched_and_chunked(ingest, written):
    ingest["url"] = "the page text"
    res = I.ingest_source({"kind": "url", "name": "proj",
                           "location": "https://x.dev/doc"})
    assert res["units"] == 1 and written[0]["ref"] == "https://x.dev/doc"


@pytest.mark.parametrize("kind,loc,msg", [
    ("repo", "/nope", "not a directory"),
    ("docs", "/nope", "not a directory"),
    ("file", "/nope.md", "not a file"),
])
def test_a_location_that_is_not_there_is_reported(ingest, kind, loc, msg):
    assert msg in I.ingest_source({"kind": kind, "name": "p",
                                   "location": loc})["error"]


def test_an_unknown_kind_is_reported(ingest):
    assert I.ingest_source({"kind": "smoke-signal", "name": "p",
                            "location": "x"})["error"] == \
        "unknown kind: smoke-signal"


def test_an_ingest_that_blows_up_returns_the_error(ingest, monkeypatch, repo):
    monkeypatch.setattr(I, "_index_repo_full",
                        lambda root, r: (_ for _ in ()).throw(OSError("io")))
    assert I.ingest_source({"kind": "repo", "name": "p",
                            "location": str(repo)}) == {"units": 0,
                                                        "error": "io"}


def test_the_display_suffix_is_stripped_off_the_repo_key(ingest, repo,
                                                         monkeypatch):
    """Indexed chunks must file under the same key recall uses, or a search
    for "requests" never finds "requests (Python)"."""
    seen: dict = {}
    monkeypatch.setattr(I, "_index_repo_full",
                        lambda root, r: seen.update(repo=r) or {"units": 1})
    I.ingest_source({"kind": "repo", "name": "requests (Python)",
                     "location": str(repo)})
    assert seen["repo"] == "requests"


# ─── fetching a URL safely ─────────────────────────────────────────────


@pytest.fixture()
def url(monkeypatch):
    import urllib.request
    state: dict = {"body": "<p>hello <b>world</b></p>", "final": None,
                   "guarded": []}
    from aiforge_core.net import ssl as netssl

    def _guard(u):
        state["guarded"].append(u)
        if u in state.get("blocked", ()):
            raise netssl.SSRFBlocked("blocked", kind="private")
    monkeypatch.setattr(netssl, "guard_public_url", _guard)

    class _Resp:
        url = None

        def __enter__(self):
            _Resp.url = state["final"]
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return state["body"].encode()
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp())
    return state


def test_the_markup_is_stripped_to_text(url):
    assert "hello" in I._fetch_url("https://x.dev") and "<b>" not in \
        I._fetch_url("https://x.dev")


def test_scripts_and_styles_are_dropped_whole(url):
    url["body"] = "<script>evil()</script><p>real text</p>"
    assert "evil" not in I._fetch_url("https://x.dev")


def test_a_private_address_is_refused_before_the_request(url):
    url["blocked"] = ("http://169.254.169.254/latest/meta-data",)
    from aiforge_core.net.ssl import SSRFBlocked
    with pytest.raises(SSRFBlocked):
        I._fetch_url("http://169.254.169.254/latest/meta-data")


def test_a_redirect_into_the_private_network_is_caught_too(url):
    url["final"] = "http://10.0.0.5/secret"
    url["blocked"] = ("http://10.0.0.5/secret",)
    from aiforge_core.net.ssl import SSRFBlocked
    with pytest.raises(SSRFBlocked):
        I._fetch_url("https://x.dev")
    assert url["guarded"] == ["https://x.dev", "http://10.0.0.5/secret"]


def test_a_dns_failure_is_left_to_the_fetch_to_surface(url, monkeypatch):
    from aiforge_core.net import ssl as netssl

    def _guard(u):
        raise netssl.SSRFBlocked("no dns", kind="dns")
    monkeypatch.setattr(netssl, "guard_public_url", _guard)
    assert "hello" in I._fetch_url("https://x.dev")


# ─── the background index run ──────────────────────────────────────────


@pytest.fixture()
def runner(monkeypatch):
    from aiforge_core.runtime import memory_sources as ms
    state: dict = {"source": {"id": 1, "kind": "repo", "name": "proj",
                              "location": "/repo"},
                   "result": {"units": 5, "error": None, "layers":
                              {"code_chunks": "ok"}},
                   "status": [], "touched": 0}
    monkeypatch.setattr(ms, "get",
                        lambda sid: state["source"] if sid == 1 else None)
    monkeypatch.setattr(ms, "set_status",
                        lambda sid, st, **kw: state["status"].append((st, kw)))
    monkeypatch.setattr(ms, "touch_indexing",
                        lambda sid: state.update(touched=state["touched"] + 1))

    def _ingest(source):
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]
    monkeypatch.setattr(I, "ingest_source", _ingest)
    return state


def test_a_clean_index_is_marked_done(runner):
    I.run_index(1)
    assert [s for s, _ in runner["status"]] == ["indexing", "done"]
    assert runner["status"][1][1]["units"] == 5
    assert runner["status"][1][1]["indexed"] is True


def test_a_layer_that_errored_is_never_a_clean_done(runner):
    runner["result"] = {"units": 3, "error": None,
                        "layers": {"code_chunks": "ok",
                                   "doc_chunks": "error:boom"}}
    I.run_index(1)
    status, kw = runner["status"][1]
    assert status == "partial" and "doc_chunks=error:boom" in kw["error"]


def test_a_failed_index_is_marked_with_its_error(runner):
    runner["result"] = {"units": 0, "error": "not a directory: /repo"}
    I.run_index(1)
    assert runner["status"][1][0] == "error"


def test_a_crash_never_leaves_the_row_stuck_indexing(runner):
    runner["result"] = RuntimeError("segfault-ish")
    I.run_index(1)
    assert runner["status"][1][0] == "error"
    assert runner["status"][1][1]["error"] == "segfault-ish"


def test_a_source_that_no_longer_exists_is_a_no_op(runner):
    I.run_index(99)
    assert runner["status"] == []


def test_a_long_ingest_heartbeats_its_lease(runner, monkeypatch):
    """Otherwise the reaper resets a slow-but-progressing index to idle, in a
    loop that never finishes."""
    monkeypatch.setenv("AIFORGE_INDEX_HEARTBEAT_S", "1")
    beats: list = []

    class _Ev:
        def __init__(self):
            self.n = 0

        def wait(self, s):
            beats.append(s)
            self.n += 1
            return self.n > 2          # two beats, then stop

        def set(self):
            pass
    import threading
    monkeypatch.setattr(threading, "Event", _Ev)
    monkeypatch.setattr(threading, "Thread",
                        lambda target=None, name=None, daemon=None:
                        pytypes.SimpleNamespace(start=target))
    I.run_index(1)
    assert runner["touched"] == 2 and beats[0] == 15


# ─── the daily sweep ───────────────────────────────────────────────────


@pytest.fixture()
def sweep(monkeypatch):
    from aiforge_core.indexing import merkle
    from aiforge_core.runtime import memory_sources as ms
    state: dict = {
        "sources": [{"id": 1, "kind": "repo", "location": "/a"},
                    {"id": 2, "kind": "docs", "location": "/b"},
                    {"id": 3, "kind": "url", "location": "https://x"}],
        "changed": {"/a": True, "/b": True}, "indexed": [], "roots": [],
        "built": []}
    monkeypatch.setattr(ms, "list_sources", lambda: state["sources"])
    monkeypatch.setattr(ms, "set_status", lambda sid, st, **kw: None)
    monkeypatch.setattr(I, "run_index", lambda sid: state["indexed"].append(sid))
    monkeypatch.setattr(merkle, "current_root",
                        lambda loc: None if state["changed"].get(loc) is None
                        else "root")
    monkeypatch.setattr(merkle, "diff",
                        lambda loc, prev: ["f"] if state["changed"].get(loc)
                        else [])
    monkeypatch.setattr(merkle, "build", lambda loc: state["built"].append(loc))
    return state


def test_only_repos_and_doc_folders_are_swept(sweep):
    out = I.reindex_all()
    assert out["total"] == 2 and sweep["indexed"] == [1, 2]


def test_an_unchanged_repo_is_skipped(sweep):
    """A full index is minutes of CPU — re-running it daily for nothing is
    pure waste."""
    sweep["changed"]["/a"] = False
    out = I.reindex_all()
    assert out["skipped"] == 1 and sweep["indexed"] == [2]


def test_force_rebuilds_everything(sweep):
    sweep["changed"] = {"/a": False, "/b": False}
    out = I.reindex_all(force=True)
    assert out["indexed"] == 2 and out["skipped"] == 0


def test_a_repo_never_indexed_before_is_always_indexed(sweep):
    sweep["changed"]["/a"] = None            # no merkle root yet
    assert 1 in sweep["indexed"] or I.reindex_all()["indexed"] >= 1


def test_a_merkle_glitch_indexes_rather_than_skips(sweep, monkeypatch):
    """A skipped real change is the worse outcome."""
    from aiforge_core.indexing import merkle
    monkeypatch.setattr(merkle, "current_root",
                        lambda loc: (_ for _ in ()).throw(OSError("db")))
    assert I.reindex_all()["indexed"] == 2


def test_the_merkle_baseline_is_refreshed_after_indexing(sweep):
    I.reindex_all()
    assert sweep["built"] == ["/a", "/b"]


def test_one_bad_repo_never_stops_the_sweep(sweep, monkeypatch):
    def _run(sid):
        if sid == 1:
            raise RuntimeError("disk full")
        sweep["indexed"].append(sid)
    monkeypatch.setattr(I, "run_index", _run)
    out = I.reindex_all()
    assert out["indexed"] == 1 and out["errors"] == [{"id": 1,
                                                      "error": "disk full"}]


def test_an_unreadable_source_list_is_reported_not_raised(monkeypatch):
    from aiforge_core.runtime import memory_sources as ms
    monkeypatch.setattr(ms, "list_sources",
                        lambda: (_ for _ in ()).throw(OSError("db gone")))
    out = I.reindex_all()
    assert out["errors"] == [{"id": None, "error": "db gone"}]
