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


def test_mvnd_run_still_gets_the_project_jdk(monkeypatch, tmp_path):
    """A plain `mvn` rewritten to mvnd must still get the JDK the pom needs:
    the JVM-build check missed `mvnd`, so a Java 24 service compiled under the
    Java 21 on PATH and failed every build ("class file has wrong version")."""
    monkeypatch.delenv("AIFORGE_CHAT_FAST_MAVEN", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_MVND", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_AUTO_JDK", raising=False)
    (tmp_path / "pom.xml").write_text("<project><properties><java.version>24</java.version>"
                                      "</properties></project>")
    import shutil
    from aiforge_core.runtime.chat_agent._turn import _env_guard
    monkeypatch.setattr(shutil, "which", lambda n: "/opt/mvnd" if n == "mvnd" else "/usr/bin/java")
    monkeypatch.setattr(_shell, "_default_java_major", lambda: 21)
    monkeypatch.setattr(_env_guard, "installed_jdks", lambda: [(21, "/jdk21"), (24, "/jdk24")])
    monkeypatch.setattr(_shell, "_jdk_key", lambda cwd: str(tmp_path))
    monkeypatch.setattr(_shell, "_test_reuse", lambda cmd, cwd: (None, None, None))
    _shell._JDK_FOR.pop(str(tmp_path), None)
    _shell._JDK_CHECKED.discard(str(tmp_path))
    ran = []
    monkeypatch.setattr(_shell, "_t_run_command_once",
                        lambda a, cwd: ran.append(a["cmd"]) or {"code": 0, "stdout": "", "stderr": ""})
    _shell._t_run_command({"cmd": f"cd {tmp_path} && mvn -o -q test -Dtest=OrderTest"}, str(tmp_path))
    assert ran and "JAVA_HOME=/jdk24" in ran[0] and "/opt/mvnd" in ran[0]
    assert _shell._JVM_BUILD.search("/home/u/.local/bin/mvnd -o test")
