"""The test guard also notices a changed SETUP line of an existing test.

Live case (real PosClientBackend job): the change re-keyed accounts; rather
than keep the old behaviour, the model edited an existing test's mock
(``.append("bankId", "L1")`` -> ``.append("accountNumber", ...)``). No
assertion moved, its run went green, the real test still failed.
"""
from aiforge_core.runtime.chat_agent._turn._test_guard import lost_assertions

JAVA_BEFORE = '''
class FooTest {
    @Test
    void carriesDates() {
        when(mongo.aggregate(any())).thenReturn(Flux.just(new Document("_id", new Document("businessId", "b1").append("bankId", "L1")).append("n", 4)));
        assertThat(result.getPending()).isEqualTo(4);
    }
}
'''


def test_java_mock_change_is_reported():
    after = JAVA_BEFORE.replace('.append("bankId", "L1")', '.append("accountNumber", "501")')
    lost = lost_assertions(JAVA_BEFORE, after)
    assert len(lost) == 1 and lost[0].startswith("(changed test setup)") and '"L1"' in lost[0]


def test_new_test_and_reformat_are_not_reported():
    added = JAVA_BEFORE.replace("}\n}\n", "}\n    @Test\n    void more() {\n        x = parse(\"a\");\n"
                                           "        assertThat(x).isNull();\n    }\n}\n")
    assert lost_assertions(JAVA_BEFORE, added) == []
    assert lost_assertions(JAVA_BEFORE, JAVA_BEFORE.replace("(any())", "( any() )")) == []


def test_rust_input_change_is_reported():
    before = 'fn adds() {\n    let v = parse("1.0.0-beta");\n    assert!(!req.matches(&v));\n}\n'
    after = before.replace("1.0.0-beta", "1.0.0")
    assert any(ln.startswith("(changed test setup)") for ln in lost_assertions(before, after))


def test_python_fixture_change_is_reported():
    before = "def test_repr():\n    f = Field(name='a')\n    assert repr(f) == 'Field(a)'\n"
    after = before.replace("name='a'", "name='a', extra=1")
    assert any(ln.startswith("(changed test setup)") for ln in lost_assertions(before, after))
    assert lost_assertions(before, before + "\ndef test_new():\n    g = 1\n    assert g\n") == []
