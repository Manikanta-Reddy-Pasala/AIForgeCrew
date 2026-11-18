"""Shared dependency caches are never wiped unless the request asks."""
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _project_delete_guard as g


def _r(cmd, goal="fix the total on the party ledger"):
    return g.refusal(SimpleNamespace(goal=goal, cwd="/tmp"), "run_command", {"cmd": cmd}, "/tmp")


def test_cache_wipes_and_purges_are_refused():
    for cmd in ("rm /home/ai/.m2/repository/com/acme/model/0.5.65/model-0.5.65.jar",
                "cd /r && rm -rf ~/.m2/repository/com/acme",
                "rm -rf $HOME/.gradle/caches",
                "find ~/.cargo/registry -name '*.crate' -delete",
                "mvn dependency:purge-local-repository -o -Dmanual=model -Daction=clean",
                "go clean -modcache", "npm cache clean --force", "pip cache purge",
                "sudo rm -rf /root/.m2"):
        r = _r(cmd)
        assert r and r["blocked"] == "shared_dependency_cache", cmd


def test_reads_project_cleans_and_asked_clears_pass():
    for cmd in ("unzip -l ~/.m2/repository/com/acme/model/0.5.65/model-0.5.65.jar",
                "find ~/.m2 -name 'model*.jar'", "ls ~/.gradle/caches",
                "mvn -o -q clean test", "cargo clean", "rm -rf target build"):
        r = _r(cmd)
        assert not (r and r.get("blocked") == "shared_dependency_cache"), cmd
    assert _r("rm -rf ~/.m2/repository/com/acme", goal="clear the maven cache for acme") is None
