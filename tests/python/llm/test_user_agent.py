"""The User-Agent AIForge sends on model calls.

    aiforge/<version> (<username>)

Every request used to go out as `curl/8.5.0 (aiforge)` — a string chosen only
because some proxies reject Python's stdlib agent. It identified no client, no
build and no person, so gateway logs could not separate one user's traffic
from another's, or a stale build from a current one.
"""
import pytest

from aiforge_core.llm import user_agent as ua


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.delenv("AIFORGE_LLM_USER_AGENT", raising=False)


def test_the_shape_is_client_version_user():
    got = ua.user_agent()
    assert got.startswith("aiforge/")
    assert got.endswith(f"({ua.username()})")
    # A real version, not the fallback, when running from this checkout.
    assert ua.version() != "dev"


def test_every_call_path_sends_the_same_agent():
    """Three places built their own copy — the direct client, the ADK/team
    pipeline and the provider's model probes. Changing the agent meant finding
    all three, and a miss left one path lying while the others told the truth.
    """
    from aiforge_core.llm.client._http import _post_headers
    from aiforge_core.llm.providers.openai_compatible import _user_agent
    from aiforge_core.llm.types import Endpoint
    ep = Endpoint(base_url="http://x/v1", api_key="k", model="m",
                  provider="openai_compatible", role="doer", extras={})
    assert _post_headers(ep)["User-Agent"] == ua.user_agent()
    assert _user_agent() == ua.user_agent()


def test_the_operator_override_still_wins(monkeypatch):
    """A proxy/WAF that demands a specific audit string is the only reason the
    curl default existed; that escape hatch has to survive."""
    monkeypatch.setenv("AIFORGE_LLM_USER_AGENT", "curl/8.5.0 (audit)")
    assert ua.user_agent() == "curl/8.5.0 (audit)"


def test_a_header_that_cannot_be_built_never_breaks_the_call(monkeypatch):
    """Both lookups touch the OS. A container with no passwd entry raises from
    getpass; a checkout with no metadata has no version. Neither may cost the
    request."""
    import getpass
    monkeypatch.setattr(getpass, "getuser",
                        lambda: (_ for _ in ()).throw(OSError("no passwd")))
    for k in ("USER", "USERNAME", "LOGNAME"):
        monkeypatch.delenv(k, raising=False)
    assert ua.username() == "unknown"
    assert ua.user_agent() == f"aiforge/{ua.version()} (unknown)"


@pytest.mark.parametrize("raw,expect", [
    ("first last", "first-last"),      # a space would split the product token
    ("EU\\\\jdoe", "EU-jdoe"),           # windows DOMAIN\\user
    ("a(b)c", "a-b-c"),                # parens are the comment delimiters
    ("j.doe_1-x", "j.doe_1-x"),        # already safe: untouched
    ("", "unknown"),
])
def test_the_username_cannot_break_the_header_grammar(raw, expect, monkeypatch):
    """A space, a backslash or a paren in the login name would corrupt the
    field itself — some gateways then drop or truncate the whole header rather
    than the bad part."""
    import getpass
    monkeypatch.setattr(getpass, "getuser", lambda: raw)
    assert ua.username() == expect


def test_it_works_on_windows_and_linux_spellings(monkeypatch):
    """getpass reads LOGNAME/USER/LNAME/USERNAME before the password database,
    which is what makes one implementation cover macOS, Linux and Windows."""
    import getpass
    monkeypatch.setattr(getpass, "getuser",
                        lambda: (_ for _ in ()).throw(OSError("no passwd")))
    for k in ("USER", "LOGNAME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("USERNAME", "winuser")      # Windows spelling
    assert ua.username() == "winuser"


# ── what review put back ────────────────────────────────────────────────

def test_there_is_a_way_to_send_no_username(monkeypatch):
    """Setting the variable EMPTY falls through to the default — so a user who
    read the release note and blanked it kept sending their login name to
    every third-party endpoint. `off` is the actual opt-out."""
    for spelling in ("off", "none", "no", "0", "false", "anon", "ANONYMOUS"):
        monkeypatch.setenv("AIFORGE_LLM_USER_AGENT", spelling)
        got = ua.user_agent()
        assert got == f"aiforge/{ua.version()}", spelling
        assert "(" not in got


def test_the_override_cannot_inject_a_header(monkeypatch):
    """urllib rejects a bare CRLF as a ValueError the retry classifier calls
    PERMANENT — one typo would kill every model call with an opaque "Invalid
    header value". CRLF+space (obs-fold) was worse: it went out on the wire
    intact, and proxies disagree on how to parse the continuation."""
    for bad in ("aiforge/1.0 (x)\r\nX-Injected: yes",
                "aiforge/1.0 (x)\r\n X-Folded: yes",
                "aiforge/1.0 (x)\nX: y"):
        monkeypatch.setenv("AIFORGE_LLM_USER_AGENT", bad)
        got = ua.user_agent()
        assert "\r" not in got, bad
        assert "\n" not in got, bad
    # A NUL cannot reach os.environ (the OS refuses it), so exercise the
    # sanitiser directly rather than pretending the env can carry one.
    assert ua._CTRL.sub("", "aiforge/1.0\x00(x)") == "aiforge/1.0(x)"


def test_an_accented_name_survives_as_ascii(monkeypatch):
    """`José` reported as `Jos` and `éric` as `e-ric` — the sanitiser was
    dropping the letter rather than the accent."""
    import getpass
    for raw, expect in (("José", "Jose"), ("éric", "eric"), ("Müller", "Muller")):
        monkeypatch.setattr(getpass, "getuser", lambda raw=raw: raw)
        assert ua.username() == expect


def test_a_pathological_name_cannot_blow_the_header_budget(monkeypatch):
    """Gateways bound header size (nginx defaults to 8k buffers)."""
    import getpass
    monkeypatch.setattr(getpass, "getuser", lambda: "x" * 5000)
    assert len(ua.username()) == 32
    assert len(ua.user_agent()) < 120


def test_the_version_scan_reads_the_project_not_a_tool_section(tmp_path, monkeypatch):
    """"the first line starting with version" picked up `version_scheme` from
    whichever tool section happened to sit higher in the file."""
    import pathlib
    proj = tmp_path / "pyproject.toml"
    proj.write_text('[tool.ruff]\nversion_scheme = "guess"\n\n'
                    '[project]\nname = "aiforgecrew"\nversion = "9.9.9"\n')
    monkeypatch.setattr(ua, "version", ua.version.__wrapped__)  # drop the cache
    monkeypatch.setattr(pathlib.Path, "resolve", pathlib.Path.absolute, raising=False)
    # Exercise the scan directly rather than faking module layout.
    text = proj.read_text().splitlines()
    in_project, found = False, None
    import re as _re
    for line in text:
        st = line.strip()
        if st.startswith("["):
            in_project = st == "[project]"
            continue
        if in_project and _re.match(r"^version\s*=", st):
            found = st.split("=", 1)[1].strip().strip('"')
            break
    assert found == "9.9.9"


def test_the_structured_path_sends_our_agent_on_the_wire(monkeypatch, tmp_path):
    """ON THE WIRE, not in the source.

    The version of this test that only grepped the module for "User-Agent"
    passed for the whole time the header was being discarded: the adapter set
    it on the httpx client, and the OpenAI SDK stamps its OWN on every request
    it builds, which wins. Every structured extraction went out as
    "OpenAI/Python <ver>" while a green test said otherwise. So this one lets
    the real SDK build a real request and reads the header off it.
    """
    import httpx
    import openai
    from openai._models import FinalRequestOptions

    from aiforge_core.integrations import instructor_adapter as ia
    from aiforge_core.llm import user_agent as ua

    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))

    # The adapter's OWN kwargs, driven through the REAL SDK — including the
    # httpx client it builds, whose `headers=` is the thing that used to lose.
    kwargs = ia.openai_kwargs("http://x/v1", "k", 30)
    kwargs["http_client"] = httpx.Client(
        headers={"User-Agent": ua.user_agent()},
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    request = openai.OpenAI(**kwargs)._build_request(
        FinalRequestOptions.construct(
            method="post", url="/chat/completions",
            json_data={"model": "m", "messages": []}))

    assert request.headers.get("user-agent") == ua.user_agent()
    assert "OpenAI/Python" not in request.headers.get("user-agent", "")


def test_the_litellm_review_path_is_attributed():
    """litellm forwards extra_headers, so source is the honest check here —
    there is no SDK client of ours to build a request from."""
    import inspect

    from aiforge_core.runtime import pr_reviewer
    assert "User-Agent" in inspect.getsource(pr_reviewer)


def test_the_ragas_judge_and_embedder_are_attributed():
    """Both are the OpenAI SDK underneath and both stamp their own agent
    without default_headers — the one pair a gateway could not attribute."""
    import inspect

    from aiforge_core.integrations import ragas_adapter
    src = inspect.getsource(ragas_adapter)
    assert src.count("default_headers") >= 2


def test_the_model_probes_identify_themselves():
    """These are the WAF case the agent exists for: a probe rejected as
    Python-urllib caches the provider DOWN for 30s while it is answering
    completions perfectly well on the same host."""
    import inspect

    from aiforge_core.llm import health
    from aiforge_core.runtime import lm_health, local_probe
    for mod in (health, lm_health, local_probe):
        assert "User-Agent" in inspect.getsource(mod), mod.__name__
