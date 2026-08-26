"""The outbound filter, rule by rule, in both directions.

Both directions matter more than coverage does: a rule that blocks everything
passes a one-sided test and silently stops the fleet learning anything, which is
the harder failure to notice.
"""
from __future__ import annotations

import pytest

from aiforge_core.memory.sync import redact


def node(body: str, title: str = "Invoice parser rounding", **meta) -> dict:
    return {"meta": {"key": "O-01", "origin": "ms", "title": title, **meta},
            "body": body}


# ── secrets: blocked ─────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    "deploy key AKIAIOSFODNN7EXAMPLE is in the CI vars",
    "token ghp_16C7e42F292c6912E7710c838347Ae178B4a1b",
    "github_pat_11ABCDEFG0aBcDeFgHiJkL_mNoPqRsTuVwXyZ0123456789abcdefghij",
    "slack hook xoxb-2401-4567-abcdefghijklmnopqrstuvwx",
    "maps key AIzaSyD-1234567890abcdefghijklmnopqrstu",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW",
    "run with password=hunter2ThatIsReal",
    "export API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD",
    "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789'",
    "psql postgres://admin:s3cr3tp4ss@db.internal:5432/oneshell",
])
def test_a_credential_blocks_the_whole_node(body):
    v = redact.review(node(body))
    assert v.send is False, f"expected a block for: {body[:40]}"
    assert v.rule.startswith("secrets.")


def test_a_credential_in_the_title_is_caught_too():
    """One text extraction for every rule, so a title cannot be the way out."""
    v = redact.review(node("nothing here", title="key AKIAIOSFODNN7EXAMPLE"))
    assert v.send is False
    assert v.rule == "secrets.aws_key"


def test_the_reason_never_quotes_the_secret():
    """The block log is written to disk; it must not become the leak."""
    v = redact.review(node("password=hunter2ThatIsReal"))
    assert "hunter2ThatIsReal" not in v.reason


# ── secrets: allowed ─────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    "the AKIA prefix identifies an AWS access key id — see `deploy/env.py`",
    "set `password=` from the vault, never inline — see `deploy/env.py`",
    "PASSWORD is read from the environment in `aiforge_core/config/env.py`",
    "commit 9f8e7d6c5b4a39281706f5e4d3c2b1a098765432 fixed `loop.py`",
    "id 3f2504e0-4f89-11d3-9a0c-0305e82c3301 in the `Parties` collection",
    "api_key=${VAULT_KEY} is the correct form in `docker-compose.yml`",
    "api_key=changeme in the sample `config.yaml` — replace it at deploy time",
])
def test_prose_about_credentials_is_not_a_credential(body):
    v = redact.review(node(body))
    assert v.send is True, f"wrongly blocked by {v.rule}: {body[:50]}"


# ── noise: blocked ───────────────────────────────────────────────────────

@pytest.mark.parametrize("title,body", [
    ("What is the capital of France", "Paris."),
    ("Brazil", "A country in South America. Its capital is Brasilia and its "
               "population is around 215 million people spread over 26 states."),
    ("How do I centre a div", ""),
    ("Weather", "It is 24 degrees today."),
    ("Note", "ok"),
])
def test_an_idle_search_does_not_leave_the_machine(title, body):
    v = redact.review(node(body, title=title))
    assert v.send is False, f"expected a block for: {title}"
    assert v.rule.startswith("noise.")


# ── noise: allowed ───────────────────────────────────────────────────────

@pytest.mark.parametrize("title,body", [
    ("Invoice parser rounding",
     "`PosClientBackend` rounds the item discount before tax, which leaks a "
     "rupee into Round Off; the fix is in `aiforge_core/memory/sync/loop.py`."),
    ("NATS retry backoff",
     "The changestream consumer retries at 5s, 30s then 120s and after that "
     "terms the message to the dead letter queue, `changestream-dlq`."),
    ("MongoDbService is mandatory",
     "Never query MongoDB directly from a service — every read and write goes "
     "through `MongoDbService`, which is the only component holding the driver."),
    ("Sonar S3776",
     "Cognitive complexity must stay at or under 15; `run_chat_agent` was "
     "decomposed into a state namespace and a yield-from control protocol."),
])
def test_real_project_knowledge_syncs(title, body):
    v = redact.review(node(body, title=title))
    assert v.send is True, f"wrongly blocked by {v.rule}: {title}"


# ── private ──────────────────────────────────────────────────────────────

def test_a_locally_scoped_note_stays_local():
    v = redact.review(node("the parser lives in `x/y.py` and it took a while "
                           "to find because the stack trace pointed elsewhere",
                           scope="local"))
    assert v.send is False
    assert v.rule == "private.scope"


@pytest.mark.parametrize("scope", ["global", "project", "", None])
def test_every_other_scope_syncs(scope):
    v = redact.review(node(
        "the parser lives in `x/y.py`; `run_once()` is where the retry budget "
        "is spent, which is why a slow admin stalls the compaction behind it",
        scope=scope))
    assert v.send is True, f"wrongly blocked by {v.rule}"


def test_a_note_only_about_a_home_path_stays_local():
    v = redact.review(node(
        "my dotfiles live in /Users/manip and I keep the older ones around "
        "in a second directory that I sync by hand every so often when I "
        "remember to, which is not very often at all these days",
        title="Dotfiles"))
    assert v.send is False
    assert v.rule == "private.home_path"


def test_a_home_path_alongside_project_knowledge_still_syncs():
    v = redact.review(node(
        "the venv is at /Users/manip/.venv but the fix is in "
        "`aiforge_core/memory/sync/loop.py` — `run_once()` reads the role "
        "before the url, which is what stops an admin pushing to itself"))
    assert v.send is True, f"wrongly blocked by {v.rule}"


# ── the stage contract ───────────────────────────────────────────────────

def test_the_filter_fails_closed(monkeypatch):
    """A rule that raises means we cannot vouch for the node, so it stays."""
    def _boom(_node):
        raise RuntimeError("bad regex")

    monkeypatch.setattr(redact, "_STAGES", (("secrets", _boom),))
    v = redact.review(node("anything at all"))
    assert v.send is False
    assert v.rule == "secrets.error"


def test_secrets_are_judged_before_noise():
    """A short note carrying a key must be reported as a SECRET, not as thin —
    the operator reads the rule name to decide whether to go and look."""
    v = redact.review(node("AKIAIOSFODNN7EXAMPLE", title="k"))
    assert v.rule == "secrets.aws_key"


def test_explain_lists_the_stages_in_order():
    assert [s["stage"] for s in redact.explain()] == ["secrets", "private", "noise"]


def test_the_substance_threshold_is_tunable(monkeypatch):
    """The length rule applies to notes with NO project signal — that is the
    only place length is the evidence."""
    from aiforge_core.memory.sync.redact import noise

    prose = node("a long stretch of ordinary sentences about nothing much at "
                 "all, going on for well over the default limit without ever "
                 "naming a place in the code or anything else one could go "
                 "and read for oneself later on",
                 title="Musings")
    assert noise.check(prose)[0] == "noise.no_project_signal"

    monkeypatch.setenv("AIFORGE_FILTER_MIN_SUBSTANCE", "10000")
    assert noise.check(prose)[0] == "noise.thin"


def test_project_signal_beats_the_length_rule():
    """Real knowledge is often terse. "MongoDbService is mandatory" is the most
    valuable kind of note here and must not lose to an 80-character rule."""
    from aiforge_core.memory.sync.redact import noise

    assert noise.check(node("`x/y.py` has a real fix in `run_once()`"))[0] == ""


def test_a_node_with_signal_but_no_body_is_still_thin():
    from aiforge_core.memory.sync.redact import noise

    rule, _ = noise.check(node("`x`", title="k"))
    assert rule == "noise.thin"
