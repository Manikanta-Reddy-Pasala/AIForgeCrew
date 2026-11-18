"""The model's Maven runs compile only stale files and use mvnd when present."""
from aiforge_core.runtime.chat_agent import _shell

F = _shell._MVN_FAST_FLAG


def test_flag_added_and_mvnd_used(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_FAST_MAVEN", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_MVND", raising=False)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda n: "/opt/mvnd" if n == "mvnd" else None)
    a, how = _shell._fast_maven({"cmd": "cd /r && JAVA_HOME=/j mvn -o -q test -Dtest=OrderTest"})
    assert a["cmd"] == f"cd /r && JAVA_HOME=/j /opt/mvnd {F} -o -q test -Dtest=OrderTest" and "daemon" in how
    a, _ = _shell._fast_maven({"cmd": "./mvnw -q test"})                 # wrapper: flag only
    assert a["cmd"] == f"./mvnw {F} -q test"


def test_left_alone(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_FAST_MAVEN", raising=False)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda n: None)
    monkeypatch.setattr(_shell.os.path, "isfile", lambda p: False)
    for c in ("mvn dependency:tree", "gradle test", "echo mvn test is slow",
              f"mvn {F} test", "cargo test"):
        a, how = _shell._fast_maven({"cmd": c})
        assert (a["cmd"], how) == (c, "") or c.startswith("echo") and how == ""
    a, how = _shell._fast_maven({"cmd": "mvn -q test"})                  # no mvnd: flag only
    assert a["cmd"] == f"mvn {F} -q test" and "daemon" not in how
    monkeypatch.setenv("AIFORGE_CHAT_FAST_MAVEN", "0")
    assert _shell._fast_maven({"cmd": "mvn test"})[0]["cmd"] == "mvn test"


def test_wrappers_and_env_prefixes(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_FAST_MAVEN", raising=False)
    monkeypatch.setenv("AIFORGE_CHAT_MVND", "0")
    a, _ = _shell._fast_maven({"cmd": "cd /r && timeout 300 mvn -o test"})
    assert a["cmd"] == f"cd /r && timeout 300 mvn {F} -o test"
    a, _ = _shell._fast_maven({"cmd": "echo run mvn test now"})
    assert a["cmd"] == "echo run mvn test now"
