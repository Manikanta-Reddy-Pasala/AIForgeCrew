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
    ("pytest --tb=short", "test"), ("pytest -x --tb=short -q", "test"),
    ("mvn --quiet test", "test"), ("./mvnw -q test", "test"), ("mvnw verify", "test"),
    ("./mvnw clean package -DskipTests", "build"), ("mvnw --offline compile", "build"),
])
def test_family_of_command(cmd, verb):
    assert R.family_of(cmd) == verb


@pytest.mark.parametrize("cmd", [
    "pytest tests/test_x.py::test_y", "pytest -k foo", "go test ./pkg/one",
    "ls", "git status", "python main.py", "mvn -q test -Dtest=Foo",
    "mvn test -Dtest=Foo", "./mvnw test -Dtest=Foo", "pytest --maxfail 1",
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
    # plain "<verb> is <words>" with no lead-in, = or backticks is prose, not a recipe
    "build is slow", "test is failing", "lint is broken", "deploy with care", "run is the verb",
    # a remove needs the word "recipe"
    "delete test", "remove build",
    # a tool name alone, an unknown verb, or a URL is prose too
    "run with docker", "go with docker compose", "start with npm install",
    "url is https://x", "endpoint is http://localhost:8080",
    # a lead-in word alone does not turn prose into a recipe
    "Always deploy with care.", "remember: build is broken on CI",
    "from now on test with caution", "always lint using common sense",
])
def test_parse_sentence_ignores_other_text(text):
    assert R.parse_sentence(text) is None


@pytest.mark.parametrize("text,verb,cmd", [
    ("test is pytest -q", "test", "pytest -q"),
    ("test means pytest -q tests/", "test", "pytest -q tests/"),
    ("build is make all", "build", "make all"),
    ("build is ./gradlew assemble", "build", "./gradlew assemble"),
    ("run-job is python main.py --job nightly", "run-job", "python main.py --job nightly"),
    ("from now on, test = ./t.sh", "test", "./t.sh"),
    ("remember, build is make all", "build", "make all"),
])
def test_plain_sentence_saves_when_the_command_looks_like_one(text, verb, cmd):
    assert R.parse_sentence(text) == ("save", verb, cmd)


@pytest.mark.parametrize("verb", ["", "   ", "!!!", "---"])
def test_empty_slug_is_rejected(tmp_path, verb):
    root = str(tmp_path)
    with pytest.raises(ValueError):
        R.save(root, verb, "./t.sh")
    with pytest.raises(ValueError):
        R.remove(root, verb)
    with pytest.raises(ValueError):
        R.decline(root, verb)
    assert not (tmp_path / ".aiforge").exists()


def test_empty_command_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        R.save(str(tmp_path), "test", "   ")
    assert R.get(str(tmp_path), "test") is None


def test_load_drops_rows_with_an_empty_slug_and_lowercases_aliases(tmp_path):
    d = tmp_path / ".aiforge"
    d.mkdir()
    (d / "recipes.json").write_text(json.dumps({
        "!!!": {"cmd": "./x.sh"},
        "": {"cmd": "./y.sh"},
        "test": {"cmd": "./t.sh", "aliases": ["T", " Run-All "]},
    }))
    recs = R.load(str(tmp_path))
    assert list(recs) == ["test"]
    assert recs["test"]["aliases"] == ["t", "run-all"]


def test_corrupt_store_is_moved_aside_on_first_write(tmp_path, caplog):
    d = tmp_path / ".aiforge"
    d.mkdir()
    (d / "recipes.json").write_text("{not json")
    with caplog.at_level("WARNING", logger="aiforge.recipes"):
        assert R.load(str(tmp_path)) == {}
        R.save(str(tmp_path), "test", "./t.sh")
    bad = list(d.glob("recipes.json.bad-*"))
    assert len(bad) == 1 and bad[0].read_text() == "{not json"
    assert bad[0].name.split("bad-")[1].isdigit()
    assert list(json.loads((d / "recipes.json").read_text())) == ["test"]
    assert any("recipes.json" in r.getMessage() for r in caplog.records)


def test_unreadable_store_blocks_the_write(tmp_path):
    d = tmp_path / ".aiforge"
    d.mkdir()
    (d / "recipes.json").mkdir()            # EISDIR on open
    assert R.load(str(tmp_path)) == {}      # a read stays safe
    with pytest.raises(OSError):
        R.save(str(tmp_path), "test", "./t.sh")
    with pytest.raises(OSError):
        R.remove(str(tmp_path), "test")
    assert (d / "recipes.json").is_dir() and not list(d.glob("recipes.json.bad-*"))


def test_corrupt_declined_file_is_logged_once(tmp_path, caplog):
    d = tmp_path / ".aiforge"
    d.mkdir()
    (d / "recipes-declined.json").write_text("[oops")
    steps = [{"type": "tool", "name": "run_command", "args": {"cmd": "pytest"}, "result": {"ok": True}}]
    with caplog.at_level("WARNING", logger="aiforge.recipes"):
        assert R.offers(str(tmp_path), steps) == [{"verb": "test", "cmd": "pytest", "cwd": "."}]
        assert R.offers(str(tmp_path), steps) == [{"verb": "test", "cmd": "pytest", "cwd": "."}]
    hits = [r for r in caplog.records if "recipes-declined.json" in r.getMessage()]
    assert len(hits) == 1
    assert (d / "recipes-declined.json").read_text() == "[oops"   # a read never moves it


def test_block_text_follows_the_replace_switch(tmp_path, monkeypatch):
    R.save(str(tmp_path), "test", "./t.sh")
    assert "the harness replaces a raw command with it" in R.block(str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_RECIPE_REPLACE", "0")
    block = R.block(str(tmp_path))
    assert "not a raw equivalent" in block and "replaces a raw command" not in block


def test_alias_that_means_a_builtin_verb_is_rejected(tmp_path):
    root = str(tmp_path)
    for alias in ("tests", "run tests", "compile", "Build It"):
        with pytest.raises(ValueError):
            R.save(root, "run-job", "python main.py", aliases=[alias])
    assert R.get(root, "run-job") is None
    # a verb's own phrase is not a collision
    R.save(root, "test", "./t.sh", aliases=["tests"])
    assert R.get(root, "test")["aliases"] == ["tests"]


def test_builtin_verbs_win_over_custom_aliases(tmp_path):
    d = tmp_path / ".aiforge"
    d.mkdir()
    (d / "recipes.json").write_text(json.dumps({
        "job": {"cmd": "./job.sh", "aliases": ["run tests", "job"]},
        "test": {"cmd": "./t.sh"},
    }))
    assert R.verb_for_phrase(str(tmp_path), "run tests") == "test"
    assert R.verb_for_phrase(str(tmp_path), "job") == "job"


@pytest.mark.parametrize("text", [
    "please just run tests", "ok please run the tests", "run tests,", "Tests.",
])
def test_polite_prefixes_are_stripped_repeatedly(tmp_path, text):
    R.save(str(tmp_path), "test", "./t.sh")
    assert R.verb_for_phrase(str(tmp_path), text) == "test"


def test_offers_carry_the_step_cwd(tmp_path):
    steps = [{"type": "tool", "name": "run_command", "args": {"cmd": "npm test", "cwd": "web"},
              "result": {"ok": True, "code": 0}}]
    assert R.offers(str(tmp_path), steps) == [{"verb": "test", "cmd": "npm test", "cwd": "web"}]


@pytest.mark.parametrize("cwd,want", [
    ("{root}/web", "web"), ("{root}/web/app/", "web/app"), ("{root}", "."),
    ("{root}/../elsewhere", "."), ("/elsewhere", "."), ("web/../..", "."), ("../up", "."),
    ("web/../api", "api"), ("", "."),
])
def test_offer_cwd_is_relative_and_inside_the_project(tmp_path, cwd, want):
    root = str(tmp_path)
    steps = [{"type": "tool", "name": "run_command", "args": {"cmd": "npm test", "cwd": cwd.format(root=root)},
              "result": {"ok": True, "code": 0}}]
    assert R.offers(root, steps) == [{"verb": "test", "cmd": "npm test", "cwd": want}]


@pytest.mark.parametrize("cwd", ["/abs/path", "../up", "web/../..", "/"])
def test_cwd_outside_the_project_is_rejected(tmp_path, cwd):
    with pytest.raises(ValueError):
        R.save(str(tmp_path), "test", "./t.sh", cwd=cwd)
    assert R.get(str(tmp_path), "test") is None
    R.save(str(tmp_path), "test", "./t.sh", cwd="web")
    assert R.get(str(tmp_path), "test")["cwd"] == "web"


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


def test_load_drops_rows_whose_cwd_leaves_the_project(tmp_path):
    d = tmp_path / ".aiforge"
    d.mkdir()
    (d / "recipes.json").write_text(json.dumps({
        "test": {"cmd": "./t.sh", "cwd": "../out"},
        "build": {"cmd": "make", "cwd": "/abs/dir"},
        "lint": {"cmd": "ruff", "cwd": "sub"},
    }))
    assert list(R.load(str(tmp_path))) == ["lint"]


@pytest.mark.parametrize("cwd", [".", "", None, "./"])
def test_recipe_command_at_the_root_is_the_bare_command(cwd):
    assert R.recipe_command({"cmd": "./t.sh", "cwd": cwd}, "/repo") == "./t.sh"


def test_recipe_command_in_a_folder_changes_into_it():
    assert R.recipe_command({"cmd": "./t.sh", "cwd": "svc"}, "/repo") == "cd /repo/svc && ./t.sh"
    assert R.recipe_command({"cmd": "./t.sh", "cwd": "my svc"}, "/re po") == "cd '/re po/my svc' && ./t.sh"


def test_exec_root_is_the_checkout_of_cwd_or_cwd_itself(tmp_path):
    import subprocess
    plain = tmp_path / "plain"
    plain.mkdir()
    assert R.exec_root(str(plain)) == str(plain)
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert os.path.realpath(R.exec_root(str(repo / "sub"))) == os.path.realpath(str(repo))


def test_offer_cwd_is_relative_to_the_checkout_the_command_ran_in(tmp_path):
    """A chat worktree's absolute cwd is made relative to the worktree, not to
    the main repo the store lives under (which would give
    ".aiforge-worktrees/…" — a folder that does not exist in the worktree)."""
    main = tmp_path / "proj"
    wt = main / ".aiforge-worktrees" / "chat-1" / "work"
    (wt / "svc").mkdir(parents=True)
    steps = [{"type": "tool", "name": "run_command",
              "args": {"cmd": "pytest -q", "cwd": str(wt / "svc")},
              "result": {"ok": True, "code": 0}}]
    assert R.offers(str(main), steps, run_root=str(wt)) == [
        {"verb": "test", "cmd": "pytest -q", "cwd": "svc"}]
