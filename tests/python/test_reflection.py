from aiforge_core.reflection import parse_reflection_xml, ReflectionResult


def test_parse_valid_xml():
    xml = """<reflection>
      <facts>
        <fact kind="convention">Repo uses Spring WebFlux.</fact>
        <fact kind="constraint">All writes through MongoDbService.</fact>
      </facts>
      <recipes>
        <recipe title="Push sync">
          <when>Saving to Docker PosClientBackend.</when>
          <how>publishToRemoteServer then NATS.</how>
        </recipe>
      </recipes>
    </reflection>"""
    r = parse_reflection_xml(xml)
    assert isinstance(r, ReflectionResult)
    assert len(r.facts) == 2
    assert r.facts[0].kind == "convention"
    assert r.facts[0].text.startswith("Repo uses")
    assert len(r.recipes) == 1
    assert r.recipes[0].title == "Push sync"


def test_parse_missing_sections():
    xml = "<reflection></reflection>"
    r = parse_reflection_xml(xml)
    assert r.facts == []
    assert r.recipes == []


def test_parse_malformed_returns_empty():
    r = parse_reflection_xml("not xml at all")
    assert r.facts == []
    assert r.recipes == []
