"""Context trimming and run setup for the ADK pipeline.

The trimming exists because ADK replays every tool result and LLM response on
every turn: ONE-117 hit an MLX GPU OOM after ~140 calls as the prompt
approached 131K tokens. Two things make the trim safe. The seed user message
is always kept (the ticket + memory brief live there), and the split point is
adjusted so a function RESPONSE is never orphaned from its call — an orphan is
a 400 from the endpoint, not a smaller prompt.

The run caps come from the same place: a local model made 383 calls over 52
minutes on ONE-7 and wrote zero files, so the whole run has a hard call
ceiling and a deadline, and an abort recovers PARTIAL state rather than
crashing.
"""
from __future__ import annotations

import os
import types as pytypes

import pytest
from google.genai import types as gtypes

from aiforge_core.runtime.adk_runner import _pipeline as pl


def _c(role, text):
    return gtypes.Content(role=role, parts=[gtypes.Part.from_text(text=text)])


def _fr(name, payload):
    return gtypes.Content(role="user", parts=[
        gtypes.Part.from_function_response(name=name, response=payload)])


# ─── the budget ────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [("50", 50), ("junk", 12), (None, 12)])
def test_int_env_falls_back(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("AIFORGE_X", raising=False)
    else:
        monkeypatch.setenv("AIFORGE_X", raw)
    assert pl._int_env("AIFORGE_X", 12) == expected


def test_the_context_window_comes_from_runtime_settings(monkeypatch):
    from aiforge_core.config import runtime_settings as rs
    monkeypatch.setattr(rs, "get", lambda key: 32768)
    assert pl._context_window() == 32768


def test_an_unreadable_setting_falls_back(monkeypatch):
    from aiforge_core.config import runtime_settings as rs
    monkeypatch.setattr(rs, "get",
                        lambda key: (_ for _ in ()).throw(RuntimeError("no db")))
    assert pl._context_window() == 131072


def test_the_history_fraction_is_shared_with_the_simple_loop(monkeypatch):
    """Team mode condenses at the same point, so a small model does not run
    near-full where it drifts and invents edits."""
    import aiforge_core.runtime.chat_agent._context._window as w
    monkeypatch.setattr(w, "_history_fraction", lambda: 0.4)
    assert pl._history_frac() == 0.4


def test_the_limits_are_read_once(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONTEXT_MAX_CONTENTS", "20")
    monkeypatch.setenv("AIFORGE_CONTEXT_KEEP_INVOCATIONS", "3")
    monkeypatch.setenv("AIFORGE_CONTEXT_MAX_TOKENS", "1000")
    monkeypatch.setenv("AIFORGE_CONTEXT_MIN_KEEP", "2")
    lim = pl._CtxLimits()
    assert lim.max_contents == 20
    assert lim.keep_invocations == 3
    assert lim.max_chars == 4000                 # 1000 tokens × 4 chars
    assert lim.min_keep == 4                     # floored


def test_the_char_ceiling_has_a_floor(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONTEXT_MAX_TOKENS", "10")
    assert pl._CtxLimits().max_chars == 4000


# ─── reading contents ──────────────────────────────────────────────────


def test_text_is_joined_across_parts():
    c = gtypes.Content(role="user", parts=[gtypes.Part.from_text(text="a"),
                                           gtypes.Part.from_text(text="b")])
    assert pl._text_of(c) == "a b"


def test_a_content_we_cannot_read_has_no_text():
    assert pl._text_of(object()) == ""


def test_a_tool_result_counts_toward_the_weight():
    assert pl._content_chars(_fr("read", {"content": "x" * 100})) > 100


def test_an_empty_content_weighs_nothing():
    assert pl._content_chars(gtypes.Content(role="user", parts=[])) == 0


# ─── the duplicate seed ────────────────────────────────────────────────


def test_an_echoed_seed_is_dropped():
    """single_turn nodes append their seed to the SHARED session events, so
    every chat agent replays the ticket+memory seed twice back to back."""
    out = pl._dedupe_adjacent_user([_c("user", "seed"), _c("user", "seed"),
                                    _c("model", "ok"), _c("user", "seed")])
    assert [pl._text_of(c) for c in out] == ["seed", "ok", "seed"]


def test_different_user_messages_are_both_kept():
    out = pl._dedupe_adjacent_user([_c("user", "a"), _c("user", "b")])
    assert len(out) == 2


# ─── capping one content ───────────────────────────────────────────────


def test_a_long_string_keeps_its_head_and_tail():
    out = pl._shorten("A" + "x" * 5000 + "Z", 2000)
    assert out.startswith("A")
    assert out.endswith("Z")
    assert "truncated" in out
    assert len(out) < 5000


def test_a_short_string_is_untouched():
    assert pl._shorten("short", 100) == "short"


def test_an_oversized_text_part_is_truncated():
    part = pl._capped_part(gtypes.Part.from_text(text="x" * 5000), 100, gtypes)
    assert "truncated" in part.text


def test_a_fat_tool_response_is_truncated_field_by_field():
    p = gtypes.Part.from_function_response(name="read",
                                           response={"content": "x" * 9000,
                                                     "ok": True})
    out = pl._capped_part(p, 2000, gtypes)
    assert "truncated" in out.function_response.response["content"]
    assert out.function_response.response["ok"] is True
    assert out.function_response.name == "read"


def test_a_small_part_passes_through():
    p = gtypes.Part.from_text(text="small")
    assert pl._capped_part(p, 1000, gtypes) is p


def test_a_content_within_the_cap_is_the_same_object():
    c = _c("user", "small")
    assert pl._cap_content(c, 1000) is c


def test_a_zero_cap_disables_capping():
    c = _c("user", "x" * 5000)
    assert pl._cap_content(c, 0) is c


def test_an_oversized_content_is_rebuilt():
    out = pl._cap_content(_c("user", "x" * 9000), 1000)
    assert out is not None
    assert "truncated" in pl._text_of(out)


def test_a_structure_we_cannot_rebuild_is_never_dropped(monkeypatch):
    class _Weird:
        parts = [pytypes.SimpleNamespace(text="x" * 9000)]
        role = "user"

        def __iter__(self):
            raise RuntimeError
    weird = _Weird()
    monkeypatch.setattr(gtypes, "Content",
                        lambda **kw: (_ for _ in ()).throw(TypeError("bad")))
    assert pl._cap_content(weird, 10) is weird


# ─── the window ────────────────────────────────────────────────────────


def _adjust(contents, split):
    return split


def _is_human(c):
    return getattr(c, "role", "") == "user"


def test_the_seed_survives_the_window():
    contents = [_c("user", "seed")] + [_c("model", str(i)) for i in range(20)]
    out = pl._window(contents, 5, _adjust, _is_human)
    assert pl._text_of(out[0]) == "seed"
    assert len(out) == 6


def test_a_short_history_is_kept_whole():
    contents = [_c("user", "a"), _c("model", "b")]
    assert pl._window(contents, 10, _adjust, _is_human) == contents


def test_the_split_is_adjusted_so_a_response_is_not_orphaned():
    """An orphaned function response is a 400 from the endpoint, not a
    smaller prompt."""
    contents = [_c("user", "seed")] + [_c("model", str(i)) for i in range(10)]
    seen: dict = {}

    def _adj(cs, split):
        seen["split"] = split
        return split - 1
    out = pl._window(contents, 3, _adj, _is_human)
    assert seen["split"] == 8
    assert len(out) == 5      # seed + 4 from the earlier split


def test_a_failing_adjuster_falls_back_to_the_raw_split():
    contents = [_c("user", "seed")] + [_c("model", str(i)) for i in range(10)]

    def _adj(cs, split):
        raise RuntimeError("bad index")
    assert len(pl._window(contents, 3, _adj, _is_human)) == 4


# ─── the tail trimmer ──────────────────────────────────────────────────


@pytest.fixture
def limits(monkeypatch):
    for k in ("AIFORGE_CONTEXT_MAX_CONTENTS", "AIFORGE_CONTEXT_MAX_TOKENS",
              "AIFORGE_CONTEXT_MAX_PART_CHARS", "AIFORGE_CONTEXT_MIN_KEEP",
              "AIFORGE_CONDENSER_STRATEGY", "AIFORGE_CONTEXT_FILTER_DISABLE"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_the_trimmer_keeps_the_seed_and_the_tail(limits):
    limits.setenv("AIFORGE_CONTEXT_MAX_CONTENTS", "4")
    trim = pl._tail_trimmer(pl._CtxLimits(), _adjust, _is_human)
    contents = [_c("user", "seed")] + [_c("model", str(i)) for i in range(10)]
    out = trim(contents)
    assert pl._text_of(out[0]) == "seed"
    assert len(out) == 5
    assert pl._text_of(out[-1]) == "9"


def test_the_trimmer_shrinks_further_under_the_token_budget(limits):
    limits.setenv("AIFORGE_CONTEXT_MAX_CONTENTS", "20")
    limits.setenv("AIFORGE_CONTEXT_MAX_TOKENS", "1000")     # → 4000 chars
    limits.setenv("AIFORGE_CONTEXT_MIN_KEEP", "4")
    trim = pl._tail_trimmer(pl._CtxLimits(), _adjust, _is_human)
    contents = [_c("user", "seed")] + [_c("model", "x" * 900) for _ in range(20)]
    out = trim(contents)
    assert len(out) < 21


def test_the_trimmer_never_goes_below_the_floor(limits):
    limits.setenv("AIFORGE_CONTEXT_MAX_CONTENTS", "20")
    limits.setenv("AIFORGE_CONTEXT_MAX_TOKENS", "1")
    trim = pl._tail_trimmer(pl._CtxLimits(), _adjust, _is_human)
    contents = [_c("user", "seed")] + [_c("model", "x" * 5000) for _ in range(20)]
    assert len(trim(contents)) >= 4


def test_zero_max_contents_keeps_everything(limits):
    limits.setenv("AIFORGE_CONTEXT_MAX_CONTENTS", "0")
    limits.setenv("AIFORGE_CONTEXT_MAX_TOKENS", "100000")
    trim = pl._tail_trimmer(pl._CtxLimits(), _adjust, _is_human)
    contents = [_c("user", "seed")] + [_c("model", str(i)) for i in range(5)]
    assert len(trim(contents)) == 6


# ─── the condenser layer ───────────────────────────────────────────────


def test_events_are_flattened_for_the_condenser():
    assert pl._as_events([_c("user", "hi")]) == [
        {"type": "content", "role": "user", "text": "hi"}]


def test_an_amortized_condense_prepends_one_synthetic_block(monkeypatch):
    import aiforge_core.runtime.condensers as cond
    monkeypatch.setattr(cond, "condense",
                        lambda events, strategy: [
                            {"role": "condenser", "text": "the story so far"},
                            events[-1]])
    filt = pl._condensing_filter(lambda cs: cs, "amortized")
    out = filt([_c("user", "a"), _c("model", "b")])
    assert pl._text_of(out[0]) == "the story so far"
    assert pl._text_of(out[-1]) == "b"


def test_a_keep_tail_condense_returns_real_contents(monkeypatch):
    import aiforge_core.runtime.condensers as cond
    monkeypatch.setattr(cond, "condense", lambda events, strategy: events[-1:])
    contents = [_c("user", "a"), _c("model", "b")]
    out = pl._condensing_filter(lambda cs: cs, "recent")(contents)
    assert out == contents[-1:]


def test_a_condense_that_keeps_nothing(monkeypatch):
    import aiforge_core.runtime.condensers as cond
    monkeypatch.setattr(cond, "condense", lambda events, strategy: [])
    assert pl._condensing_filter(lambda cs: cs, "recent")([_c("user", "a")]) == []


# ─── plugin wiring ─────────────────────────────────────────────────────


def test_the_context_filter_can_be_disabled(limits):
    limits.setenv("AIFORGE_CONTEXT_FILTER_DISABLE", "1")
    assert pl._build_context_plugins() == []


def test_the_filter_and_the_phantom_guard_are_wired(limits):
    plugins = pl._build_context_plugins()
    assert plugins
    assert any(type(p).__name__ == "ContextFilterPlugin"
                           for p in plugins)
    assert any(type(p).__name__ == "PhantomToolGuardPlugin" for p in plugins)


def test_a_condenser_strategy_layers_over_the_trim(limits):
    limits.setenv("AIFORGE_CONDENSER_STRATEGY", "amortized")
    assert pl._build_context_plugins()


def test_an_unavailable_guard_is_not_fatal(monkeypatch):
    import aiforge_core.runtime.tool_error_plugin as tep
    monkeypatch.delattr(tep, "PhantomToolGuardPlugin", raising=False)
    assert pl._phantom_tool_guard() == []


# ─── run configuration ─────────────────────────────────────────────────


def test_the_whole_run_has_a_call_ceiling(monkeypatch):
    """ONE-7 made 383 calls in 52 minutes and wrote zero files."""
    monkeypatch.setenv("AIFORGE_MAX_LLM_CALLS", "42")
    assert pl._pipeline_run_config().max_llm_calls == 42


def test_the_single_agent_run_is_capped_too(monkeypatch):
    monkeypatch.setenv("AIFORGE_MAX_LLM_CALLS", "7")
    kwargs = pl._run_kwargs("sess-1", _c("user", "hi"))
    assert kwargs["session_id"] == "sess-1"
    assert kwargs["run_config"].max_llm_calls == 7


def test_stateful_tools_are_keyed_to_the_run(monkeypatch):
    """Otherwise each mints a per-call id and leaks — browser and ipython did
    exactly that."""
    from aiforge_core.runtime.tools import bash
    seen: list = []
    monkeypatch.setattr(bash, "set_run_id", lambda sid: seen.append(("bash", sid)))
    pl._key_stateful_tools("sess-1")
    assert ("bash", "sess-1") in seen


def test_a_missing_stateful_tool_is_skipped(monkeypatch):
    from aiforge_core.runtime.tools import bash
    monkeypatch.setattr(bash, "set_run_id",
                        lambda sid: (_ for _ in ()).throw(RuntimeError("no tmux")))
    pl._key_stateful_tools("sess-1")


def test_every_run_resource_is_torn_down(monkeypatch):
    from aiforge_core.runtime.tools import bash
    seen: list = []
    monkeypatch.setattr(bash, "destroy_session", lambda sid: seen.append(sid))
    pl._destroy_run_resources("sess-1")
    assert seen == ["sess-1"]


def test_a_failing_teardown_still_returns(monkeypatch):
    from aiforge_core.runtime.tools import bash
    monkeypatch.setattr(bash, "destroy_session",
                        lambda sid: (_ for _ in ()).throw(RuntimeError("no tmux")))
    pl._destroy_run_resources("sess-1")


# ─── ticket state ──────────────────────────────────────────────────────


def _ticket(**kw):
    base = {"id": 1, "identifier": "ONE-1", "title": "Fix it", "body": "details",
            "project": "app", "metadata": {}}
    base.update(kw)
    return pytypes.SimpleNamespace(**base)


@pytest.fixture
def quiet_state(monkeypatch):
    monkeypatch.setattr(pl, "_toolchain_md", lambda: "")
    monkeypatch.setattr(pl, "_user_prefs_md", lambda: "")
    monkeypatch.setattr(pl, "_emit_rules_injected", lambda t, s: None)


def test_the_raw_ask_is_seeded_for_the_enhancer_guard(quiet_state):
    """The guard restores it when the enhancer's rewrite collapsed."""
    state = pl._ticket_state(_ticket(), [], "", "")
    assert state["raw_ask"] == "Fix it\ndetails"
    assert state["ticket_identifier"] == "ONE-1"


def test_the_operators_scope_allowlist_is_seeded_and_kept(quiet_state):
    """scope_guard / verify_scope / the Validator all judged an empty field
    without this."""
    t = _ticket(metadata={"scope_allowlist_globs": "app/**\n\ntests/**\n"})
    state = pl._ticket_state(t, [], "", "")
    assert state["scope_allowlist_globs"] == ["app/**", "tests/**"]
    assert state["scope_allowlist_globs_seeded"] == ["app/**", "tests/**"]


def test_rules_and_memory_are_seeded_as_state_not_prompt_text(quiet_state):
    state = pl._ticket_state(_ticket(), [], "RULES", "MEMORY")
    assert state["rules_md"] == "RULES"
    assert state["memory_brief_md"] == "MEMORY"


def test_the_toolchain_and_preferences_ride_along(monkeypatch):
    monkeypatch.setattr(pl, "_toolchain_md", lambda: "use ./mvnw")
    monkeypatch.setattr(pl, "_user_prefs_md", lambda: "prefers tabs")
    monkeypatch.setattr(pl, "_emit_rules_injected", lambda t, s: None)
    state = pl._ticket_state(_ticket(), [], "", "")
    assert state["toolchain_md"] == "use ./mvnw"
    assert state["user_prefs_md"] == "prefers tabs"


@pytest.mark.parametrize("raw,expected", [
    ("a/**\n\nb/**", ["a/**", "b/**"]),
    (["a/**", "", None], ["a/**"]),
    (None, []),
    (42, []),
])
def test_glob_lists_are_normalised(raw, expected):
    assert pl._glob_list(raw) == expected


# ─── repo rules ────────────────────────────────────────────────────────


def test_repo_rules_are_collected_from_the_ticket_text(monkeypatch):
    from aiforge_core.runtime import repo_rules
    seen: dict = {}

    def _collect(root, seed, query):
        seen.update(root=root, seed=seed, query=query)
        return "RULES", []
    monkeypatch.setattr(repo_rules, "collect_or_ask", _collect)
    monkeypatch.setattr(pl, "_emit_ambiguous_rule_notice", lambda t, a: None)
    assert pl._collect_repo_rules(_ticket(), ["app/**"]) == "RULES"
    assert "Fix it" in seen["query"]
    assert seen["seed"] == ["app/**"]


def test_a_broken_rules_collection_is_not_fatal(monkeypatch):
    from aiforge_core.runtime import repo_rules
    monkeypatch.setattr(repo_rules, "collect_or_ask",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("bad glob")))
    assert pl._collect_repo_rules(_ticket(), []) == ""


def test_an_ambiguous_rule_match_is_surfaced_not_blocked(monkeypatch):
    """An autonomous ticket never blocks on this — the notice is the only
    human-visible signal it produces."""
    events: list = []
    monkeypatch.setattr(pl.tickets_mod, "add_event",
                        lambda tid, role, kind, body, meta: events.append((kind, body)))
    rule = pytypes.SimpleNamespace(name="python-style")
    pl._emit_ambiguous_rule_notice(_ticket(), [[rule, rule]])
    assert events[0][0] == "ambiguous_rule_match"


def test_an_interactive_ticket_was_already_asked(monkeypatch):
    monkeypatch.setattr(pl.tickets_mod, "add_event",
                        lambda *a: pytest.fail("notified an interactive ticket"))
    pl._emit_ambiguous_rule_notice(_ticket(metadata={"interactive": True}),
                                   [[pytypes.SimpleNamespace(name="r")]])


def test_no_ambiguity_emits_nothing(monkeypatch):
    monkeypatch.setattr(pl.tickets_mod, "add_event",
                        lambda *a: pytest.fail("emitted with nothing ambiguous"))
    pl._emit_ambiguous_rule_notice(_ticket(), [])


def test_a_failed_notice_does_not_stop_the_remaining_groups(monkeypatch):
    calls = {"n": 0}

    def _add(*a):
        calls["n"] += 1
        raise RuntimeError("db down")
    monkeypatch.setattr(pl.tickets_mod, "add_event", _add)
    rule = pytypes.SimpleNamespace(name="r")
    pl._emit_ambiguous_rule_notice(_ticket(), [[rule], [rule]])
    assert calls["n"] == 2


def test_the_applied_rules_are_recorded_for_the_workflow_view(monkeypatch):
    from aiforge_core.runtime import observability as obs
    from aiforge_core.runtime import repo_rules
    seen: dict = {}
    monkeypatch.setattr(repo_rules, "matched_names", lambda root, seed: ["python"])
    monkeypatch.setattr(obs, "emit_context_injected",
                        lambda **kw: seen.update(kw))
    pl._emit_rules_injected(_ticket(), ["app/**"])
    assert seen == {"ticket_id": 1, "agent_role": "pipeline", "rules": ["python"]}


def test_no_matched_rules_emits_nothing(monkeypatch):
    from aiforge_core.runtime import observability as obs
    from aiforge_core.runtime import repo_rules
    monkeypatch.setattr(repo_rules, "matched_names", lambda root, seed: [])
    monkeypatch.setattr(obs, "emit_context_injected",
                        lambda **kw: pytest.fail("emitted with no rules"))
    pl._emit_rules_injected(_ticket(), [])


# ─── toolchain + preferences ───────────────────────────────────────────


def test_the_toolchain_brief_is_host_verified(monkeypatch):
    from aiforge_core.config import repo_standards as rstd
    monkeypatch.setattr(rstd, "toolchain_brief", lambda root: "python3, ./mvnw")
    assert pl._toolchain_md() == "python3, ./mvnw"


def test_a_broken_toolchain_probe_is_empty(monkeypatch):
    from aiforge_core.config import repo_standards as rstd
    monkeypatch.setattr(rstd, "toolchain_brief",
                        lambda root: (_ for _ in ()).throw(OSError("no path")))
    assert pl._toolchain_md() == ""


def test_preferences_come_from_both_stores(monkeypatch):
    from aiforge_core.runtime import user_prefs
    import aiforge_core.runtime.chat_agent as ca
    monkeypatch.setattr(user_prefs, "preferences_block", lambda: "global prefs")
    monkeypatch.setattr(ca, "_preferences_context", lambda root: "repo prefs")
    assert pl._user_prefs_md() == "global prefs\n\nrepo prefs"


def test_a_broken_preference_store_is_skipped(monkeypatch):
    from aiforge_core.runtime import user_prefs
    import aiforge_core.runtime.chat_agent as ca
    monkeypatch.setattr(user_prefs, "preferences_block",
                        lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    monkeypatch.setattr(ca, "_preferences_context", lambda root: "repo prefs")
    assert pl._user_prefs_md() == "repo prefs"


# ─── images ────────────────────────────────────────────────────────────


def test_image_attachments_are_injected_for_a_vision_doer(monkeypatch):
    import aiforge_core.config.agent_config as ac
    import aiforge_core.runtime.vision_adk as va
    monkeypatch.setattr(ac, "load_all", lambda: {"doer": {"model": "vlm"}})
    seen: dict = {}

    def _inject(contents, model, images):
        seen.update(model=model, images=images)
        return [_c("user", "with images")]
    monkeypatch.setattr(va, "inject_image_parts", _inject)
    t = _ticket(metadata={"attached_files": [
        {"name": "shot.PNG", "path": "/a.png"},
        {"name": "spec.pdf", "path": "/a.pdf"}]})
    out = pl._with_images(_c("user", "seed"), t, gtypes)
    assert pl._text_of(out) == "with images"
    assert seen["images"] == ["/a.png"]
    assert seen["model"] == "vlm"


def test_a_ticket_with_no_images_is_untouched(monkeypatch):
    import aiforge_core.config.agent_config as ac
    monkeypatch.setattr(ac, "load_all", lambda: {"doer": {"model": "vlm"}})
    content = _c("user", "seed")
    assert pl._with_images(content, _ticket(), gtypes) is content


def test_a_failing_injection_keeps_the_original(monkeypatch):
    import aiforge_core.config.agent_config as ac
    monkeypatch.setattr(ac, "load_all",
                        lambda: (_ for _ in ()).throw(RuntimeError("no config")))
    content = _c("user", "seed")
    assert pl._with_images(content, _ticket(), gtypes) is content


# ─── trajectory dump ───────────────────────────────────────────────────


@pytest.fixture
def session():
    return pytypes.SimpleNamespace(id="sess-1", events=[{"e": 1}],
                                   state={"k": "v"})


def test_the_trajectory_is_dumped_for_replay(monkeypatch, session):
    import aiforge_core.runtime.trajectory as tj
    seen: dict = {}
    monkeypatch.setattr(tj, "dump_trajectory",
                        lambda tid, sid, events, state: seen.update(
                            tid=tid, sid=sid, events=events, state=state))
    monkeypatch.delenv("AIFORGE_TRAJECTORY_DUMP", raising=False)
    pl._dump_trajectory(session, None, {"ticket_identifier": "ONE-1"})
    assert seen["tid"] == "ONE-1"
    assert seen["sid"] == "sess-1"
    assert seen["state"] == {"k": "v"}


def test_a_run_without_a_ticket_dumps_under_unknown(monkeypatch, session):
    import aiforge_core.runtime.trajectory as tj
    seen: dict = {}
    monkeypatch.setattr(tj, "dump_trajectory",
                        lambda tid, sid, events, state: seen.update(tid=tid))
    monkeypatch.delenv("AIFORGE_TRAJECTORY_DUMP", raising=False)
    pl._dump_trajectory(session, None, {})
    assert seen["tid"] == "unknown"


def test_the_dump_can_be_turned_off(monkeypatch, session):
    import aiforge_core.runtime.trajectory as tj
    monkeypatch.setenv("AIFORGE_TRAJECTORY_DUMP", "0")
    monkeypatch.setattr(tj, "dump_trajectory",
                        lambda *a: pytest.fail("dumped with the gate off"))
    pl._dump_trajectory(session, None, {})


def test_a_failed_dump_is_swallowed(monkeypatch, session):
    import aiforge_core.runtime.trajectory as tj
    monkeypatch.delenv("AIFORGE_TRAJECTORY_DUMP", raising=False)
    monkeypatch.setattr(tj, "dump_trajectory",
                        lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    pl._dump_trajectory(session, None, {})
