"""Which built UI the API serves.

There is one place: the checkout's own ``web/dist``. An earlier installer also
staged a copy into the package as ``aiforge_core/web_dist``; that untracked
copy stayed behind in checkouts and, searched first, froze the UI at whenever
it was made — new API, old screens, no error anywhere. It must never win.
"""
from __future__ import annotations

import os

from aiforge_core.api import api


def test_the_checkout_build_is_served_and_nothing_else():
    import aiforge_core
    repo = os.path.dirname(os.path.dirname(os.path.abspath(aiforge_core.__file__)))
    assert api._resolve_dist() == os.path.join(repo, "web", "dist")


def test_no_packaged_copy_is_consulted():
    import inspect

    from aiforge_core.api import _ui_serving
    assert "web_dist" not in inspect.getsource(_ui_serving._resolve_dist)
