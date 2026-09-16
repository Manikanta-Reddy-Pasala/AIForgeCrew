"""The printed URL must say which machines can actually open it.

"run.sh finishes, the page won't connect" is what a loopback bind looks like
from any other machine — and the URL on screen gives no hint, so the next thing
an operator tries is --host 0.0.0.0, which the boot guard then refuses for
having no token.
"""
from __future__ import annotations

import pytest

from aiforge_core.cli.serve import reachability


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_a_loopback_bind_says_so_and_says_how_to_widen(host):
    lines = "\n".join(reachability(host, 8799))
    assert "THIS machine only" in lines
    assert "ssh -L 8799:127.0.0.1:8799" in lines
    # the widening hint must carry the token, or the next attempt is refused
    assert "AIFORGE_API_TOKEN" in lines
    assert "--host 0.0.0.0" in lines


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_a_wildcard_bind_says_it_is_open(host):
    assert "any machine" in " ".join(reachability(host, 8799))


def test_a_specific_interface_names_it():
    assert "192.168.70.115" in " ".join(reachability("192.168.70.115", 8799))
