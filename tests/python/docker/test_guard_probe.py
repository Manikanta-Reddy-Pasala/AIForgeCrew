"""docker/guard-probe.py: only an immediate refusal (the guard's TCP reset)
or no route counts as blocked — a timeout means the packet left the machine."""
import errno
import runpy
import socket
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parents[3] / "docker" / "guard-probe.py"


def _run(monkeypatch, capsys, v4, v6):
    def fake(addr, timeout=None):
        exc = v4 if ":" not in addr[0] else v6
        if exc is None:
            class _S:
                def close(self):
                    pass
            return _S()
        raise exc
    monkeypatch.setattr(socket, "create_connection", fake)
    runpy.run_path(str(PROBE), run_name="__main__")
    return capsys.readouterr().out.strip()


@pytest.mark.parametrize("v4, v6, want", [
    (ConnectionRefusedError(), ConnectionRefusedError(), "NET-BLOCKED"),
    (ConnectionRefusedError(), OSError(errno.ENETUNREACH, "no route"), "NET-BLOCKED"),
    (ConnectionRefusedError(), OSError(errno.EADDRNOTAVAIL, "no v6"), "NET-BLOCKED"),
    (OSError(errno.ENETUNREACH, "x"), OSError(errno.EADDRNOTAVAIL, "y"), "NET-NOROUTE"),
    (None, ConnectionRefusedError(), "NET-OPEN 1.1.1.1:open"),
    (socket.timeout(), ConnectionRefusedError(), "NET-OPEN 1.1.1.1:timeout"),
    (OSError(errno.EHOSTUNREACH, "icmp from upstream"), ConnectionRefusedError(),
     f"NET-OPEN 1.1.1.1:errno-{errno.EHOSTUNREACH}"),
    (ConnectionRefusedError(), None, "NET-OPEN 2606:4700:4700::1111:open"),
])
def test_probe(monkeypatch, capsys, v4, v6, want):
    assert _run(monkeypatch, capsys, v4, v6) == want


def test_both_start_checks_use_the_probe():
    root = PROBE.parents[1]
    assert "docker/guard-probe.py" in (root / "docker" / "entrypoint.sh").read_text()
    assert "docker/guard-probe.py" in (root / "run.sh").read_text()
