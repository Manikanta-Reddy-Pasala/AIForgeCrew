"""A test build missing its test framework gets the dependency to add."""
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _env_guard as E

OUT = ("[ERROR] /r/src/test/java/a/XTest.java:[3,29] package org.junit.jupiter.api does not exist\n"
       "[ERROR] /r/src/test/java/a/XTest.java:[11,35] package org.assertj.core.api does not exist")


def test_spring_boot_project_gets_the_starter(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text("<parent><artifactId>spring-boot-starter-parent</artifactId></parent>")
    monkeypatch.setattr(E, "_m2_has", lambda g, a: True)
    n = E._test_dep_note(SimpleNamespace(), OUT, str(tmp_path))
    assert "spring-boot-starter-test" in n and "<scope>test</scope>" in n and "mvn -o" in n


def test_plain_project_gets_each_artifact_once(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text("<project/>")
    monkeypatch.setattr(E, "_m2_has", lambda g, a: False)
    st = SimpleNamespace()
    n = E._test_dep_note(st, OUT, str(tmp_path))
    assert "junit-jupiter" in n and "assertj-core" in n and "Not found in ~/.m2" in n
    assert E._test_dep_note(st, OUT, str(tmp_path)) == ""          # said once


def test_other_errors_do_not_trigger(tmp_path):
    assert E._test_dep_note(SimpleNamespace(), "package com.foo.bar does not exist", str(tmp_path)) == ""
