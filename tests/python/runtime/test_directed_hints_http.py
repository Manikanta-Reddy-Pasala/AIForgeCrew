"""Directed hints must name HTTP/framework root causes so a local model can
crack them (not the vague generic 'wrong value' hint)."""
from aiforge_core.runtime.parallel_subtasks import _directed_hints


def test_308_trailing_slash_hint():
    out = ("assert response.status_code == 400\n"
           "E  assert 308 == 400\n"
           "E  where 308 = <WrapperTestResponse streamed [308 PERMANENT REDIRECT]>")
    hints = " ".join(_directed_hints(out)).lower()
    assert "trailing slash" in hints
    assert "strict_slashes" in hints
    assert "do not change the tests" in hints


def test_405_method_hint():
    hints = " ".join(_directed_hints("E assert 405 == 200  405 METHOD NOT ALLOWED")).lower()
    assert "method" in hints and "methods=" in hints


def test_import_hint_still_works():
    hints = " ".join(_directed_hints(
        "ImportError: cannot import name 'Book' from 'models'"))
    assert "Book" in hints
