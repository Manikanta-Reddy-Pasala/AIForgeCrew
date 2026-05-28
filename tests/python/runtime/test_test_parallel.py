"""Tests for build_test_command parallel helper (gap A8a)."""
from __future__ import annotations

from aiforge_core.runtime.tools.test_runner import build_test_command


def test_parallel_off_returns_base_unchanged():
    base = ["pytest", "-q"]
    assert build_test_command(
        base, framework="python", parallel=False, workers=None
    ) == base


def test_pytest_auto_workers():
    out = build_test_command(
        ["pytest", "-q"], framework="python", parallel=True, workers=None
    )
    assert out == ["pytest", "-q", "-n", "auto"]


def test_pytest_explicit_workers():
    out = build_test_command(
        ["pytest", "-q"], framework="python", parallel=True, workers=4
    )
    assert out == ["pytest", "-q", "-n", "4"]


def test_go_parallel_workers():
    out = build_test_command(
        ["go", "test", "./..."], framework="go", parallel=True, workers=8
    )
    assert out == ["go", "test", "./...", "-parallel", "8"]


def test_go_parallel_default_workers():
    out = build_test_command(
        ["go", "test", "./..."], framework="go", parallel=True, workers=None
    )
    # default to a sensible worker count when unspecified
    assert out[:3] == ["go", "test", "./..."]
    assert "-parallel" in out


def test_jest_max_workers():
    out = build_test_command(
        ["npm", "test"], framework="node", parallel=True, workers=2
    )
    assert out == ["npm", "test", "--maxWorkers=2"]


def test_jest_default_max_workers():
    out = build_test_command(
        ["npm", "test"], framework="node", parallel=True, workers=None
    )
    assert out[:2] == ["npm", "test"]
    assert any(a.startswith("--maxWorkers=") for a in out)


def test_maven_noop():
    base = ["mvn", "-q", "test"]
    assert build_test_command(
        base, framework="java-maven", parallel=True, workers=4
    ) == base


def test_cargo_noop():
    base = ["cargo", "test", "--quiet"]
    assert build_test_command(
        base, framework="rust", parallel=True, workers=4
    ) == base


def test_unknown_framework_noop():
    base = ["something", "test"]
    assert build_test_command(
        base, framework="java-gradle", parallel=True, workers=4
    ) == base


def test_does_not_mutate_base():
    base = ["pytest", "-q"]
    build_test_command(base, framework="python", parallel=True, workers=4)
    assert base == ["pytest", "-q"]
