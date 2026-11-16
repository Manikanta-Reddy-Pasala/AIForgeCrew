"""A build stopped by a too-old JDK names the installed JDKs that are new
enough, once per turn.

Live case: a fresh run on PosClientBackend (needs 24, system java 21) never
found ~/jdk24/..., never ran a test, and shipped a test that did not
compile (0/8). OpenCode broke the same job's pom.xml trying to work around it.
"""
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _env_guard as E

ERR = ("[ERROR] Failed to execute goal maven-compiler-plugin:3.14.1:compile: "
       "Fatal error compiling: error: release version 24 not supported")


def _jdk(root, name, version):
    home = root / name
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "java").write_text("")
    (home / "release").write_text(f'JAVA_VERSION="{version}"\n')
    return home


def test_names_new_enough_jdks_once(tmp_path, monkeypatch):
    j24 = _jdk(tmp_path, "jdk-24.0.2", "24.0.2")
    _jdk(tmp_path, "jdk-17", "17.0.9")
    j25 = _jdk(tmp_path, "graalce-25", "25.0.2")
    monkeypatch.setattr(E, "_JDK_GLOBS", (str(tmp_path / "*"),))
    st = SimpleNamespace()
    n = E.note(st, "run_command", {"command": "mvn -o test"}, ERR, str(tmp_path))
    assert f"JAVA_HOME={j24}" in n and f"JAVA_HOME={j25}" in n and "jdk-17" not in n
    assert "do not edit the build files" in n
    assert E.note(st, "run_command", {"command": "mvn -o test"}, ERR, str(tmp_path)) == ""


def test_none_found_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "_JDK_GLOBS", (str(tmp_path / "*"),))
    n = E.note(SimpleNamespace(), "run_command", {}, ERR, str(tmp_path))
    assert "JDK 24 or newer" in n and "none was found" in n


def test_class_file_version_maps_to_java_release():
    assert E._JDK_TOO_OLD.search("Unsupported class file major version 68")


def test_wrong_class_file_version_means_java_24():
    m = E._JDK_TOO_OLD.search("bad class file: Foo.class\n class file has wrong version 68.0, "
                              "should be 65.0")
    raw = int(next(g for g in m.groups() if g))
    assert raw - 44 == 24
