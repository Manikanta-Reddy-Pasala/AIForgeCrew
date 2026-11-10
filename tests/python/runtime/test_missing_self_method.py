"""Post-edit check: a self.method() call to a method the class never defines."""
from __future__ import annotations

import textwrap

from aiforge_core.runtime import post_edit as P


def _check(tmp_path, src):
    f = tmp_path / "m.py"
    f.write_text(textwrap.dedent(src))
    return P._missing_self_methods(str(f))


def test_the_tomlkit_case_is_reported(tmp_path):
    out = _check(tmp_path, """
        class Parser:
            def __init__(self):
                self._idx = 0
            def _parse_table(self):
                if not self._can_extend_table("a"):
                    return self._parse_item()
            def _parse_item(self):
                return self._idx
    """)
    assert len(out) == 1 and "`Parser` has no method `_can_extend_table`" in out[0]
    assert ":6:" in out[0]


def test_defined_inherited_and_assigned_names_are_fine(tmp_path):
    assert _check(tmp_path, """
        class Base:
            def helper(self):
                return 1
        class A(Base):
            handler = None
            def __init__(self, cb):
                self.cb = cb
            def run(self):
                self.helper(); self.cb(); self.handler(); self.run2()
            async def run2(self):
                return self.__class__()
            @staticmethod
            def s():
                return 1
    """) == []


def test_unknown_bases_dynamic_classes_and_other_receivers_are_skipped(tmp_path):
    assert _check(tmp_path, """
        from x import Mixin
        class A(Mixin):
            def run(self):
                return self.from_mixin()
        class B:
            def __getattr__(self, name):
                return lambda: name
            def run(self):
                return self.anything()
        class C(metaclass=Meta):
            def run(self):
                return self.made_by_meta()
        class D:
            def run(this):
                return other.missing()
    """) == []


def test_a_generic_or_protocol_base_still_checks(tmp_path):
    out = _check(tmp_path, """
        from typing import Generic, TypeVar
        T = TypeVar("T")
        class Box(Generic[T]):
            def get(self):
                return self.missing()
    """)
    assert len(out) == 1 and "`Box` has no method `missing`" in out[0]


def test_diagnostics_carry_it(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("class A:\n    def run(self):\n        return self.nope()\n")
    errs = P.diagnostics(str(f), str(tmp_path))
    assert errs is not None and any("AF001" in e for e in errs)


def test_template_methods_and_static_style_functions_are_fine(tmp_path):
    assert _check(tmp_path, """
        class Parameter:
            def __init__(self, decls):
                self.name = self._parse_decls(decls)
        class Option(Parameter):
            def _parse_decls(self, decls):
                return decls[0]
        class Replay:
            def is_spec(spec):
                return spec.startswith("x")
            @staticmethod
            def other(fh):
                fh.flush()
    """) == []


def test_mixins_are_skipped(tmp_path):
    assert _check(tmp_path, """
        import unittest
        class PeekableMixinTests:
            def test_a(self):
                self.assertEqual(1, 1)
        class PeekableTests(PeekableMixinTests, unittest.TestCase):
            pass
        class _RescueMixin:
            def go(self):
                self._record_spend()
    """) == []
