"""The registry file is parsed once per change, not once per call."""
import json
import os


def test_load_is_cached_until_the_file_changes(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.config import model_registry as mr
    path = mr._path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump([{"id": "a", "model": "m1", "context_window": 1000}], f)
    calls = []
    real = json.load
    monkeypatch.setattr(mr.json, "load", lambda f: calls.append(1) or real(f))
    assert mr.context_for("m1") == 1000
    assert mr.context_for("m1") == 1000
    assert len(calls) == 1
    rows = mr._load()
    rows[0]["context_window"] = 5                 # a caller's copy only
    assert mr.context_for("m1") == 1000
    with open(path, "w", encoding="utf-8") as f:
        json.dump([{"id": "a", "model": "m1", "context_window": 2000}], f)
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert mr.context_for("m1") == 2000


def test_a_save_is_seen_at_once(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.config import model_registry as mr
    mr._save([{"id": "a", "model": "m1", "context_window": 1}])
    assert mr.context_for("m1") == 1
    mr._save([{"id": "a", "model": "m1", "context_window": 7}])
    assert mr.context_for("m1") == 7
