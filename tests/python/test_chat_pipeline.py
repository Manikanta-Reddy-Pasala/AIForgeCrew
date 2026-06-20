from aiforge_core.runtime import chat_pipeline as cp


class _FakePart:
    def __init__(self, text=None, fc=None, fr=None):
        self.text = text
        self.function_call = fc
        self.function_response = fr


class _FC:
    def __init__(self, name, args):
        self.name = name
        self.args = args


class _FR:
    def __init__(self, name, response):
        self.name = name
        self.response = response


class _Content:
    def __init__(self, parts):
        self.parts = parts


class _Event:
    def __init__(self, author, parts):
        self.author = author
        self.content = _Content(parts)


def test_map_text_to_thought():
    ev = _Event("planner", [_FakePart(text="here is the plan")])
    out = cp.map_event(ev)
    assert out == [{"type": "thought", "text": "**planner** · here is the plan"}]


def test_map_function_call_to_tool():
    ev = _Event("doer", [_FakePart(fc=_FC("file_write", {"path": "a.py"}))])
    out = cp.map_event(ev)
    assert out[0]["type"] == "tool"
    assert out[0]["name"] == "file_write"
    assert out[0]["args"] == {"path": "a.py"}


def test_map_function_response_to_thought():
    ev = _Event("doer", [_FakePart(fr=_FR("file_write", "ok"))])
    out = cp.map_event(ev)
    assert out[0]["type"] == "thought"
    assert "file_write" in out[0]["text"]


def test_empty_parts():
    assert cp.map_event(_Event("x", [])) == []


def test_event_text_joins():
    ev = _Event("a", [_FakePart(text="foo "), _FakePart(text="bar")])
    assert cp._event_text(ev) == "foo bar"
