"""A pasted file is content to read, not a list of things to do.

Pasting a netplan file produced the checklist "part-1 10.130.212.1/24, part-2
8.8.8.8, part-3 10.10.116.80, … part-5 metric: 105" — a YAML sequence item is
written exactly like a markdown bullet, so every value in the file became an
"ask" the run then had to tick off.
"""
from __future__ import annotations

from aiforge_core.runtime.chat_agent._context._recall import _split_asks

NETPLAN = """sudo cat 01-netcfg.yaml

in here is see

network:
  ethernets:
    enp1s0:
      addresses:
        - 10.130.212.1/24
      mtu: 1500
      nameservers:
        addresses:
          - 8.8.8.8
          - 10.10.116.80
          - 10.10.110.80
      routes:
        - metric: 105
          to: 0.0.0.0/0
          via: 10.130.212.2
    enp8s0:
      addresses:
        - 192.168.122.152/24
  renderer: NetworkManager
  version: 2
"""


def test_a_pasted_yaml_file_is_not_a_checklist():
    assert _split_asks(NETPLAN) == []


def test_a_fenced_block_is_content_not_asks():
    text = ("here is the config, tell me what is wrong\n"
            "```yaml\n"
            "- first value\n"
            "- second value\n"
            "- third value\n"
            "```\n")
    assert _split_asks(text) == []


def test_the_users_own_bulleted_list_still_splits():
    text = ("- fix the login redirect\n"
            "- add a regression test for it\n"
            "- update the changelog\n")
    assert _split_asks(text) == ["fix the login redirect",
                                 "add a regression test for it",
                                 "update the changelog"]


def test_a_numbered_list_still_splits():
    text = ("1. rename the service to gateway\n"
            "2. point the health check at the new name\n")
    assert len(_split_asks(text)) == 2


def test_multi_sentence_asks_still_split():
    text = "fix the auth bug. also why does the retry loop stall? and add a test"
    parts = _split_asks(text)
    assert len(parts) >= 2


def test_a_prohibition_is_a_rule_not_a_part_of_the_work():
    # Live: "Do not edit the tests" became board item part-2; the agent spent
    # a context reset and a dozen git commands "completing" it.
    text = ("There are bugs in stats.py. Fix them until `python3 -m pytest -q` "
            "passes. Do not edit the tests. Commit when done.")
    assert _split_asks(text) == []          # one real ask: no checklist
    text = ("Create a scratch file notes.txt containing hello. Then start the "
            "dev server in the background. Don't install anything. And never "
            "push to main.")
    assert _split_asks(text) == [
        "Create a scratch file notes.txt containing hello",
        "Then start the dev server in the background"]
    text = ("- fix the login redirect\n- do not touch the session store\n"
            "- add a regression test for it\n")
    assert _split_asks(text) == ["fix the login redirect",
                                 "add a regression test for it"]


def test_a_question_that_opens_with_do_is_still_an_ask():
    parts = _split_asks("do the retries back off? also fix the timeout in client.py")
    assert parts == ["do the retries back off?", "also fix the timeout in client.py"]


def test_a_bare_value_list_is_not_asks():
    # Flat (unindented) but every item is a single token — a pasted column of
    # values, not a request.
    text = ("- 10.130.212.1/24\n"
            "- 8.8.8.8\n"
            "- 10.10.116.80\n")
    assert _split_asks(text) == []


def test_a_flat_mapping_list_is_not_asks():
    text = ("- metric: 105\n"
            "- mtu: 1500\n"
            "- version: 2\n")
    assert _split_asks(text) == []
