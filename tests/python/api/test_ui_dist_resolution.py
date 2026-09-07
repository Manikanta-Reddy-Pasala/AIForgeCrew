"""Which built UI the API serves when BOTH copies exist.

Cost a real afternoon: ``installer/build_payload.sh`` copies web/dist into the
package as ``aiforge_core/web_dist``, and that copy is untracked, so it stays
behind in the checkout for ever. It used to be searched FIRST, which meant a
freshly built UI never appeared — new API, frozen screens, and no error
anywhere. The checkout's own build has to win.
"""
from __future__ import annotations

import os

from aiforge_core.api import api


def test_the_checkout_build_wins_over_a_stale_packaged_copy(monkeypatch):
    seen = []

    def both_exist(path):
        seen.append(path)
        return path.endswith(("web/dist", "web_dist"))

    monkeypatch.setattr(os.path, "isdir", both_exist)
    assert api._resolve_dist().endswith(os.path.join("web", "dist"))


def test_an_installed_package_still_finds_its_own_copy(monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda p: p.endswith("web_dist"))
    assert api._resolve_dist().endswith("web_dist")


def test_neither_present_falls_back_to_the_checkout_path(monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda p: False)
    assert api._resolve_dist().endswith(os.path.join("web", "dist"))
