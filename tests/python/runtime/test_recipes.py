# tests/python/runtime/test_recipes.py
import json
import os
import pytest
from aiforge_core.runtime import recipes as R


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_RECIPES", "1")


def test_save_get_remove_round_trip(tmp_path):
    root = str(tmp_path)
    assert R.get(root, "test") is None
    R.save(root, "test", "./scripts/test.sh", cwd=".", aliases=["t"])
    got = R.get(root, "test")
    assert got["cmd"] == "./scripts/test.sh" and got["aliases"] == ["t"]
    data = json.loads((tmp_path / ".aiforge" / "recipes.json").read_text())
    assert "saved" in data["test"]
    assert R.remove(root, "test") is True
    assert R.get(root, "test") is None
    assert R.remove(root, "test") is False


def test_disabled_switch_hides_everything(tmp_path, monkeypatch):
    R.save(str(tmp_path), "test", "pytest -q")
    monkeypatch.setenv("AIFORGE_CHAT_RECIPES", "0")
    assert R.get(str(tmp_path), "test") is None
    assert R.load(str(tmp_path)) == {}


def test_unreadable_store_is_empty(tmp_path):
    d = tmp_path / ".aiforge"
    d.mkdir()
    (d / "recipes.json").write_text("{not json")
    assert R.load(str(tmp_path)) == {}


@pytest.mark.parametrize("cmd,verb", [
    ("pytest -q", "test"), ("python -m pytest", "test"), ("npm test", "test"),
    ("mvn -q test", "test"), ("go test ./...", "test"), ("cargo test", "test"),
    ("make test", "test"), ("mvn -q clean package -DskipTests", "build"),
    ("mvn compile", "build"), ("npm run build", "build"), ("go build ./...", "build"),
    ("cargo build", "build"), ("make", "build"), ("tsc --noEmit", "build"),
    ("ruff check .", "lint"), ("eslint .", "lint"), ("npm run lint", "lint"),
])
def test_family_of_command(cmd, verb):
    assert R.family_of(cmd) == verb


@pytest.mark.parametrize("cmd", [
    "pytest tests/test_x.py::test_y", "pytest -k foo", "go test ./pkg/one",
    "ls", "git status", "python main.py", "mvn -q test -Dtest=Foo",
])
def test_targeted_or_other_commands_have_no_family(cmd):
    assert R.family_of(cmd) is None


def test_rewrite_replaces_a_family_command_with_the_recipe(tmp_path):
    root = str(tmp_path)
    R.save(root, "test", "./scripts/test.sh")
    assert R.rewrite(root, "pytest -q") == ("test", "./scripts/test.sh")
    assert R.rewrite(root, "./scripts/test.sh") is None      # already the recipe
    assert R.rewrite(root, "pytest tests/a.py") is None      # targeted: keep
    assert R.rewrite(root, "mvn compile") is None            # no build recipe


def test_rewrite_can_be_turned_into_a_warning(tmp_path, monkeypatch):
    R.save(str(tmp_path), "test", "./t.sh")
    monkeypatch.setenv("AIFORGE_CHAT_RECIPE_REPLACE", "0")
    assert R.rewrite(str(tmp_path), "pytest") is None
    assert R.warn(str(tmp_path), "pytest") == "test"


@pytest.mark.parametrize("text,verb", [
    ("run tests", "test"), ("Run the tests.", "test"), ("tests", "test"),
    ("compile", "build"), ("build it", "build"), ("please build", "build"),
    ("run it", "run"), ("lint", "lint"), ("deploy", "deploy"),
    ("run-job", "run-job"), ("run the job", "run-job"), ("run run-job", "run-job"),
    ("nightly", "run-job"),
])
def test_bare_phrases_map_to_verbs(tmp_path, text, verb):
    root = str(tmp_path)
    for v in ("test", "build", "run", "lint", "deploy"):
        R.save(root, v, f"./{v}.sh")
    R.save(root, "run-job", "python main.py --job nightly", aliases=["run the job", "nightly"])
    assert R.verb_for_phrase(root, text) == verb


@pytest.mark.parametrize("text", [
    "run tests and fix the failures", "compile the new module I described above",
    "what does run tests do?", "run tests?", "",
])
def test_longer_messages_are_not_bare_phrases(tmp_path, text):
    R.save(str(tmp_path), "test", "./t.sh")
    R.save(str(tmp_path), "build", "./b.sh")
    assert R.verb_for_phrase(str(tmp_path), text) is None


def test_bare_phrase_without_a_recipe_is_none(tmp_path):
    assert R.verb_for_phrase(str(tmp_path), "run tests") is None


@pytest.mark.parametrize("text,verb,cmd", [
    ("remember: compile = ./build.sh", "build", "./build.sh"),
    ("always run tests with ./t.sh", "test", "./t.sh"),
    ("from now on build is make all", "build", "make all"),
    ("recipe run-job = python main.py --job nightly", "run-job", "python main.py --job nightly"),
    ("test means `pytest -q tests/`", "test", "pytest -q tests/"),
])
def test_parse_sentence_saves(text, verb, cmd):
    assert R.parse_sentence(text) == ("save", verb, cmd)


@pytest.mark.parametrize("text,verb", [
    ("forget the test recipe", "test"), ("remove recipe build", "build"),
    ("drop the run-job recipe", "run-job"),
])
def test_parse_sentence_removes(text, verb):
    assert R.parse_sentence(text) == ("remove", verb, "")


@pytest.mark.parametrize("text", [
    "run tests", "the build is slow", "compile = ./build.sh is what the README says, fix the README",
    "remember that the build uses java 21",
])
def test_parse_sentence_ignores_other_text(text):
    assert R.parse_sentence(text) is None


def test_offers_pick_successful_family_commands_without_a_recipe(tmp_path):
    root = str(tmp_path)
    R.save(root, "build", "mvn -q package")
    steps = [
        {"type": "tool", "name": "run_command", "args": {"cmd": "pytest -q"}, "result": {"ok": True, "code": 0}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "mvn -q package"}, "result": {"ok": True, "code": 0}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "ruff check ."}, "result": {"ok": False, "code": 1}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "pytest -q"}, "result": {"ok": True, "code": 0}},
        {"type": "tool", "name": "file_read", "args": {"path": "x"}, "result": {"ok": True}},
    ]
    assert R.offers(root, steps) == [{"verb": "test", "cmd": "pytest -q", "cwd": "."}]


def test_declined_verbs_are_not_offered_again(tmp_path):
    root = str(tmp_path)
    R.decline(root, "test")
    steps = [{"type": "tool", "name": "run_command", "args": {"cmd": "pytest"}, "result": {"ok": True, "code": 0}}]
    assert R.offers(root, steps) == []


def test_help_text_lists_recipes(tmp_path):
    R.save(str(tmp_path), "test", "./t.sh", aliases=["t"])
    text = R.help_text(str(tmp_path))
    assert "test → ./t.sh" in text and "aliases: t" in text
    assert R.help_text(str(tmp_path / "none")) == "No recipes saved for this project."


def test_block_is_short(tmp_path):
    for i in range(14):
        R.save(str(tmp_path), f"v{i}", f"./{i}.sh")
    block = R.block(str(tmp_path))
    assert block.startswith("RECIPES")
    assert block.count("\n") <= 12 and "more:" in block
