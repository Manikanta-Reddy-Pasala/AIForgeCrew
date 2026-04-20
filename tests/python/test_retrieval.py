from aiforge_core.retrieval import rrf_fuse, Hit


def test_rrf_fuse_reinforces_agreed():
    bm25 = [Hit(id="a", score=0.0), Hit(id="b", score=0.0), Hit(id="c", score=0.0)]
    vec  = [Hit(id="b", score=0.0), Hit(id="a", score=0.0), Hit(id="d", score=0.0)]
    merged = rrf_fuse([bm25, vec], k=60, top_n=4)
    ids = [h.id for h in merged]
    # a and b appear in both lists → should rank highest
    assert ids.index("a") < ids.index("c")
    assert ids.index("b") < ids.index("d")
