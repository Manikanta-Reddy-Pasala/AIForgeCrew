"""The isolated-mode controller writes a match-every-host filter when allowed."""
import importlib.util
import json
import pathlib


def _ctl(tmp_path, monkeypatch):
    monkeypatch.setenv("EGRESS_POLICY_DIR", str(tmp_path))
    src = pathlib.Path(__file__).resolve().parents[3] / "docker" / "isolated" / "egress-controller.py"
    spec = importlib.util.spec_from_file_location("egress_ctl_t", src)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.POLICY = str(tmp_path)
    m.HOSTS, m.ALLOW, m.CHANGES = (str(tmp_path / n) for n in ("hosts.json", "allow.txt", "changes.jsonl"))
    m.required_hosts = lambda: []
    return m


def test_allow_all_filter_and_back(tmp_path, monkeypatch):
    m = _ctl(tmp_path, monkeypatch)
    m.save(["pypi.org"], who="t")
    assert "pypi" in (tmp_path / "allow.txt").read_text()
    m.save(["pypi.org"], who="t", allow_all=True)
    assert (tmp_path / "allow.txt").read_text() == "^.*$\n"
    assert json.loads((tmp_path / "hosts.json").read_text())["allow_all"] is True
    m.save(["pypi.org"], who="t")                                     # None keeps the switch
    assert (tmp_path / "allow.txt").read_text() == "^.*$\n"
    m.save(["pypi.org"], who="t", allow_all=False)
    assert "pypi" in (tmp_path / "allow.txt").read_text()
    assert "* (allow all)" in (tmp_path / "changes.jsonl").read_text()
