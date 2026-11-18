"""A test or build run does not clear the memory of shell reads."""
from aiforge_core.runtime.chat_agent._turn._action import _runs_tests_only as t


def test_test_and_build_runs():
    for c in ('cd /r && ./gradlew :core:test --tests "com.a.FooTest" 2>&1',
              "cd /r && JAVA_HOME=/j mvn -o -q test -Dtest=X",
              "timeout 300 cargo test --test t",
              "go test ./...", "python -m pytest -q tests/test_a.py", "npm test"):
        assert t({"cmd": c}), c


def test_commands_that_may_change_files():
    for c in ("./gradlew spotlessApply", "./gradlew ktfmtFormat", "cargo fmt",
              "sed -i s/a/b/ x.kt", "git checkout x.kt", "./gradlew test && sed -i s/a/b/ x",
              "mvn clean test", "python gen.py", "npm run lint -- --fix"):
        assert not t({"cmd": c}), c
