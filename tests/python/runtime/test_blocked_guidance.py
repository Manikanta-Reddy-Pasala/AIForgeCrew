"""A blocked call tells the model to change approach, not retry: most outside
hosts are unreachable from an AIForge box by design, and a local model that
hits the wall retries, then curls, then tries a notebook cell."""
import json
import types

from aiforge_core.runtime.chat_agent import _blocked


def _st():
    return types.SimpleNamespace(blocked_hits=0)


def test_an_egress_refusal_says_use_what_is_here():
    st = _st()
    out = _blocked.for_model(st, "web_fetch", {"ok": False, "error": "host_not_allowed"})
    assert "Do NOT retry" in out["next_step"]
    assert "internal package index" in out["next_step"]
    assert st.blocked_hits == 1


def test_a_shell_network_failure_is_recognised():
    res = {"ok": False, "exit_code": 6,
           "output": "curl: (6) Could not resolve host: pypi.org"}
    out = _blocked.for_model(_st(), "run_command", res)
    assert out["next_step"].startswith("The outside network is not reachable")


def test_a_policy_block_says_pick_a_different_approach():
    out = _blocked.for_model(_st(), "run_command",
                             {"ok": False, "blocked": "risk", "error": "dangerous"})
    assert "different approach" in out["next_step"]


def test_repeated_blocks_in_a_turn_get_a_firm_stop():
    st = _st()
    _blocked.for_model(st, "web_fetch", {"ok": False, "error": "host_not_allowed"})
    out = _blocked.for_model(st, "run_command",
                             {"ok": False, "output": "Temporary failure in name resolution"})
    assert out["next_step"].startswith("This is the 2th blocked outside attempt")
    assert "STOP trying to reach outside" in out["next_step"]


def test_ordinary_failures_and_successes_are_left_alone():
    st = _st()
    assert _blocked.classify("run_command", {"ok": True, "output": "Could not resolve host"}) is None
    assert _blocked.classify("run_command", {"ok": False, "output": "SyntaxError"}) is None
    assert _blocked.classify("file_read", {"ok": False, "error": "no such file"}) is None
    res = {"ok": False, "output": "boom"}
    assert _blocked.for_model(st, "run_command", res) is res
    assert st.blocked_hits == 0


def test_the_model_sees_the_guidance_first_the_ui_sees_the_raw_result():
    from aiforge_core.runtime.chat_agent import _loop
    st = types.SimpleNamespace(convo=[], blocked_hits=0, reads_new=0, edits_made=0,
                               builder_finalized=False)
    res = {"ok": False, "error": "host_not_allowed", "hint": "x" * 50}
    g = _loop._post_tool(st, "web_fetch", {"url": "https://example.com"}, res,
                         "/tmp", "sig", 1, None, types.SimpleNamespace(skills_md=""))
    events = list(g)
    assert "next_step" not in events[0]["result"]                  # UI: raw
    obs = st.convo[-1]["content"]
    assert obs.startswith("OBSERVATION: ")
    payload = json.loads(obs[len("OBSERVATION: "):].split("\n[format")[0])
    assert list(payload)[0] == "next_step"
