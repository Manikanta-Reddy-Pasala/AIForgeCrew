"""The running build is reported, so 'is my fix deployed?' has an answer."""
from aiforge_core import build_info


def test_env_wins(monkeypatch):
    build_info.build.cache_clear()
    monkeypatch.setenv("AIFORGE_BUILD_SHA", "abc1234")
    monkeypatch.setenv("AIFORGE_BUILD_DATE", "2026-10-03")
    assert build_info.build() == {"commit": "abc1234", "date": "2026-10-03", "subject": ""}
    build_info.build.cache_clear()


def test_without_env_it_never_raises_and_always_has_a_commit(monkeypatch):
    build_info.build.cache_clear()
    monkeypatch.delenv("AIFORGE_BUILD_SHA", raising=False)
    out = build_info.build()
    assert out["commit"]
    build_info.build.cache_clear()
