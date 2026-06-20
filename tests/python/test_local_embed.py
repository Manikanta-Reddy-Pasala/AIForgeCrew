from aiforge_core.memory import local_embed as le


def test_dim_and_determinism():
    a = le.embed("configure the database connection pool")
    b = le.embed("configure the database connection pool")
    assert len(a) == le.EMBED_DIM
    assert a == b


def test_l2_normalized():
    v = le.embed("some non-empty text here")
    norm = sum(x * x for x in v) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_empty_is_zero_vector():
    v = le.embed("   ")
    assert v == [0.0] * le.EMBED_DIM
    assert le.cosine(v, le.embed("anything")) == 0.0


def test_lexical_similarity_ordering():
    q = le.embed("how to configure neo4j connection")
    near = le.embed("configuring the neo4j connection settings")
    far = le.embed("bake a chocolate cake with butter")
    assert le.cosine(q, near) > le.cosine(q, far)


def test_morphology_overlap():
    # trigram features make inflections overlap
    a = le.embed("configure")
    b = le.embed("configuring")
    c = le.embed("banana")
    assert le.cosine(a, b) > le.cosine(a, c)


def test_cosine_length_mismatch_is_zero():
    assert le.cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0
