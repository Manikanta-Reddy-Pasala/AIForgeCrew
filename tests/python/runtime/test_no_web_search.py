"""Web SEARCH is gone, and must stay gone.

Removed 2026-09-03: the query string is outbound data. Whatever the model typed
into it — a line from your logs, a symbol name, a customer name — left the box
to a third-party engine, and no filter ran on it. Reading a URL someone supplied
is a different risk (known destination, off by default), so that stayed.

These pins are cheap and they fail LOUDLY, because the failure mode we are
guarding is a well-meaning re-add: the tool is easy to reintroduce on one
surface (a registry entry, a prompt line, a role allowlist) and nothing else
would notice.
"""
from __future__ import annotations

import pathlib

import yaml

from aiforge_core.runtime import doer_tools
from aiforge_core.runtime.chat_agent import _registry
from aiforge_core.runtime.chat_agent._tools import _schemas


def test_chat_registry_has_no_search_tool():
    assert "web_search" not in _registry.TOOLS
    assert "web_search" not in _schemas.CATALOG


def test_chat_system_prompt_does_not_advertise_search():
    """Underscore AND prose. The first version of this test asserted only
    `"web_search" not in _SYSTEM`, and the prompt's own ACTION contract said
    "... and web search are all available" — with a space. It passed while the
    highest-salience line in the prompt advertised the removed tool."""
    from aiforge_core.runtime.chat_agent import _prompt

    sys_prompt = _prompt._SYSTEM
    assert "web_search" not in sys_prompt
    for claim in ("web search are all available", "and web search are",
                  "search the open web"):
        assert claim not in sys_prompt, claim
    # and it must say so once, unconditionally — not only via the intent regex
    assert "no web search" in sys_prompt.lower()


def test_doer_tool_surface_has_no_search():
    names = {getattr(t, "name", None) or t.func.__name__
             for t in doer_tools.adk_function_tools()}
    assert "web_search" not in names
    assert not hasattr(doer_tools, "web_search")


def test_no_role_allowlist_grants_search():
    from aiforge_core import agents as _agents

    spec = yaml.safe_load(
        (pathlib.Path(_agents.__file__).parent / "agents.yaml").read_text())
    for role, cfg in (spec.get("agents") or {}).items():
        allowed = ((cfg.get("tools") or {}).get("allowed") or [])
        assert "web_search" not in allowed, f"{role} still allows web_search"


def test_fetch_module_carries_no_search_backend():
    """No DDG/Tavily/Brave client may come back into the fetch module — and the
    module must not POST at all, since a body is the other way query text
    leaves."""
    from aiforge_core.runtime import mentions as _mentions
    from aiforge_core.runtime.doer_tools import _web as _dweb
    from aiforge_core.runtime.tools import web_fetch as _wf
    from aiforge_core.runtime.tools import web_ingest as _wi

    # Every module that can reach the network on the agent's behalf, not just
    # the one the search code used to live in.
    for mod in (_wf, _dweb, _wi, _mentions):
        body = pathlib.Path(mod.__file__).read_text().lower()
        for needle in ("duckduckgo", "tavily", "brave"):
            assert needle not in body, (
                f"a search backend is back in {mod.__name__}: {needle}")
    text = pathlib.Path(_wf.__file__).read_text().lower()
    # A request body is the other way a query leaves, and urllib infers POST
    # from `data=` — so grepping for the word "post" would miss it entirely.
    # Check the request construction instead.
    assert "data=" not in text, "the fetch module can send a body again"


def test_web_intent_directive_says_it_cannot_look_it_up():
    from aiforge_core.runtime.chat_agent._context._recall import _WEB_LOOKUP_DIRECTIVE

    low = _WEB_LOOKUP_DIRECTIVE.lower()
    assert "no web access" in low
    assert "web_search" not in low
    # The point is not that the word "never" appears — "you may never be able
    # to search, so answer from memory" would pass that. Pin the clauses.
    assert "cannot look it up" in low
    assert "never claim you searched" in low
    assert "as if it were checked" in low
