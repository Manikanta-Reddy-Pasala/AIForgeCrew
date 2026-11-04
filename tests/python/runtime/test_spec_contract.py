# tests/python/runtime/test_spec_contract.py
"""What a request spells out in backticks is looked for in the code before a
turn ends: a run that said "all parts completed" lacked half the API."""
from types import SimpleNamespace

from aiforge_core.runtime import spec_contract as S
from aiforge_core.runtime.chat_agent._turn import _contract_gate as G, _tasks

REQ = """Extend `shop` with:
1. New dataclass `shop.inv.Batch` with fields ...
2. `Inventory.receive(sku, quantity, ref=None, *, batch_id: str | None = None, expiry: date | None = None) -> StockLevel`
3. `MovementKind` gains `MovementKind.RESTOCK`.
4. New module `shop/invoices.py` with `issue(order, on)`.
Round with `money(x)`, use `Decimal("1.5")`, `sorted(xs)`, `Path.exists()`.
"""


def _repo(tmp_path, inv: str, extra: dict | None = None):
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "__init__.py").write_text("")
    (tmp_path / "shop" / "models.py").write_text(
        "def money(x):\n    return x\n\nclass MovementKind:\n    SALE = 'sale'\n")
    (tmp_path / "shop" / "inv.py").write_text(inv)
    for k, v in (extra or {}).items():
        (tmp_path / k).write_text(v)
    return str(tmp_path)


def test_claims_read_the_backticked_api():
    kinds = {(c["kind"], c.get("name") or c.get("path")) for c in S.claims(REQ)}
    assert ("export", "Batch") in kinds and ("method", "receive") in kinds
    assert ("member", "RESTOCK") in kinds and ("file", "shop/invoices.py") in kinds
    rec = next(c for c in S.claims(REQ) if c.get("name") == "receive")
    assert rec["params"] == ["sku", "quantity", "ref", "batch_id", "expiry"]


def test_missing_names_the_gaps_and_not_library_calls(tmp_path):
    root = _repo(tmp_path, "class Inventory:\n    def receive(self, sku, quantity, ref=None):\n        pass\n")
    gaps = S.missing(REQ, root)
    assert "Inventory.receive does not take batch_id, expiry" in gaps
    assert "shop.inv.Batch is not defined" in gaps
    assert "MovementKind.RESTOCK is missing" in gaps
    assert "shop/invoices.py does not exist" in gaps
    assert "function issue is missing" in gaps
    joined = " ".join(gaps)
    for lib in ("Decimal", "sorted", "Path", "money"):
        assert lib not in joined


def test_a_complete_tree_has_no_gaps(tmp_path):
    inv = ("from dataclasses import dataclass\n@dataclass\nclass Batch:\n    sku: str\n\n"
           "class Inventory:\n    def receive(self, sku, quantity, ref=None, *, batch_id=None, expiry=None):\n        pass\n")
    root = _repo(tmp_path, inv)
    (tmp_path / "shop" / "models.py").write_text(
        "def money(x):\n    return x\n\nclass MovementKind:\n    SALE = 'sale'\n    RESTOCK = 'restock'\n")
    (tmp_path / "shop" / "invoices.py").write_text("def issue(order, on):\n    pass\n")
    assert S.missing(REQ, root) == []


def test_kwargs_take_any_parameter_and_off_switch(tmp_path, monkeypatch):
    root = _repo(tmp_path, "class Inventory:\n    def receive(self, sku, quantity, **kw):\n        pass\n")
    assert not any("receive" in g for g in S.missing(REQ, root))
    monkeypatch.setenv("AIFORGE_CHAT_SPEC_CONTRACT", "0")
    assert S.missing(REQ, root) == []


def test_no_python_or_no_names_says_nothing(tmp_path):
    (tmp_path / "a.js").write_text("x")
    assert S.missing(REQ, str(tmp_path)) == ["shop/invoices.py does not exist"]   # files only
    assert S.missing("just fix the login bug", str(tmp_path)) == []


def test_gate_sends_back_then_lets_go(tmp_path):
    root = _repo(tmp_path, "class Inventory:\n    pass\n")
    st = SimpleNamespace(goal=REQ, cwd=root, edits_made=2, convo=[], readonly_mode=False,
                         plan_mode=False)
    outs = []
    for _ in range(4):
        gen = G.gate(st, {"text": "All done."})
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            outs.append(stop.value)
    assert outs[:3] == ["continue"] * 3 and outs[3] is None      # bounded
    assert "Inventory.receive is missing" in st.convo[0]["content"]
    st2 = SimpleNamespace(goal=REQ, cwd=root, edits_made=0, convo=[])
    assert list(G.gate(st2, {"text": "x"})) == []                  # nothing built: no check


def test_a_request_part_cannot_be_skipped_by_the_model():
    board = _tasks.seed_board(["add coupons", "add invoices"])
    res, _ = _tasks.apply_progress(board, {"slug": "part-1", "status": "skipped"})
    assert not res["ok"] and "not optional" in res["error"]
    assert board["part-1"]["status"] == "pending"
    res, _ = _tasks.apply_progress(board, {"slug": "part-1", "status": "failed"})
    assert res["ok"]                                                # a named blocker is allowed
    res, _ = _tasks.apply_progress(board, {"slug": "part-2", "status": "skipped"},
                                   allow_request_removal=True)
    assert res["ok"]                                                # the user dropped it
    res, _ = _tasks.apply_progress(board, {"slug": "extra", "title": "tidy", "status": "skipped"})
    assert res["ok"]                                                # the model's own item


def test_what_the_user_wants_gone_is_not_a_claim(tmp_path):
    root = _repo(tmp_path, "class Inventory:\n    def add_stock(self, sku):\n        pass\n")
    req = ("Remove `Inventory.legacy_receive()`, rename `Inventory.receive` to "
           "`Inventory.add_stock(sku)` and delete `shop/old.py`.")
    assert S.missing(req, root) == []


def test_inherited_self_set_and_framework_members_are_not_gaps(tmp_path):
    inv = ("class Base:\n    def save(self, force=False):\n        pass\n\n"
           "class Svc(Base):\n    def __init__(self):\n        self.items = {}\n\n"
           "from django.db import models\n"
           "class Order(models.Model):\n    pass\n\n"
           "class Plain:\n    pass\n")
    root = _repo(tmp_path, inv)
    req = ("`Svc.save(force=True)`, `Svc.items`, `Order.objects`, `Order.DoesNotExist`, "
           "`Plain.nothing`")
    assert S.missing(req, root) == ["Plain.nothing is missing"]


def test_library_paths_imports_and_varargs(tmp_path):
    inv = ("from django.shortcuts import render\n"
           "def log(msg, *args):\n    pass\n")
    root = _repo(tmp_path, inv)
    req = ("Use `django.db.models.Model`, `collections.abc.Mapping`, `render(request, t)`, "
           "`log(msg, *args)`, and the crash in `process(data)`: see `shop.missing.Thing`.")
    gaps = S.missing(req, root)
    assert gaps == ["function process is missing", "module shop.missing does not exist (for shop.missing.Thing)"]


def test_a_call_site_is_not_a_signature(tmp_path):
    root = _repo(tmp_path, "def process(payload):\n    pass\n")
    assert S.missing("`process(data)` crashes on empty input", root) == []


def test_a_mostly_js_tree_checks_files_only(tmp_path):
    for i in range(5):
        (tmp_path / f"m{i}.ts").write_text("export const x = 1\n")
    (tmp_path / "tool.py").write_text("x = 1\n")
    req = "Call `fetchData(url)` from `UserService.load()`; add `scripts/gen.py`."
    assert S.missing(req, str(tmp_path)) == ["scripts/gen.py does not exist"]


def test_a_truncated_index_tells_files_only(tmp_path, monkeypatch):
    root = _repo(tmp_path, "class Inventory:\n    pass\n")
    monkeypatch.setattr(S, "MAX_FILES", 1)
    assert S.missing(REQ, root) == ["shop/invoices.py does not exist"]


def test_strict_finish_and_plan_skip_rules():
    st = SimpleNamespace(goal=REQ, cwd="/nowhere", edits_made=1, convo=[])
    assert list(G.gate(st, {"text": "x"}, None, True)) == []          # a subtask run
    board = {"plan-1": {"title": "optional: tidy docs", "status": "pending",
                        "from_request": True}}
    res, _ = _tasks.apply_progress(board, {"slug": "plan-1", "status": "skipped"})
    assert res["ok"]


def test_the_contract_note_line_is_stripped_from_a_later_answer():
    from aiforge_core.runtime.chat_agent._turn import _finish
    text = "Done.\n\n_Missing from what the request names: Inventory.receive does not take expiry._"
    assert "Missing from" not in _finish._strip_own_notes(text)


def test_round_two_cases(tmp_path):
    inv = ("import requests\nfrom enum import Enum\n"
           "class Session(requests.Session):\n    pass\n\n"
           "class MovementKind(Enum):\n    SALE = 'sale'\n")
    root = _repo(tmp_path, inv)
    for i in range(3):
        (tmp_path / f"w{i}.js").write_text("x\n")          # mixed repo, still >30% py
    req = ("`Session.request(method, url, timeout=5)`; `MovementKind.RESTOCK` is new; "
           "`Cls.old_api` should be removed; `legacy.py` is no longer needed; "
           "call `useEffect(() => {})` in the page.")
    assert S.missing(req, root) == ["MovementKind.RESTOCK is missing"]


def test_a_steer_drops_its_gaps_and_only_drop_words_unlock_removal(tmp_path):
    root = _repo(tmp_path, "class Inventory:\n    pass\n")
    st = SimpleNamespace(goal=REQ, cwd=root, edits_made=1, convo=[],
                         steers=["skip the invoices module for now"])
    gaps = G._gaps(st)
    assert gaps and not any("invoices" in g for g in gaps)
    assert _tasks.user_dropped_work(["don't do coupons"])
    assert not _tasks.user_dropped_work(["how is it going?", "also add tests"])


def test_a_prune_that_frees_little_is_not_done(monkeypatch):
    """Ageing old outputs edits the middle of the history (a full re-read on a
    local server): only worth it when it frees a real share of the budget."""
    from aiforge_core.runtime.chat_agent._turn import _limits
    from aiforge_core.runtime.chat_agent._context import _aging
    big = "OBSERVATION: " + "x" * 6000
    convo = [{"role": "system", "content": "s"}]
    for i in range(4):
        convo += [{"role": "assistant", "content": 'ACTION: file_read\nARGS_JSON: {"path": "a%d"}' % i},
                  {"role": "user", "content": big}]
    convo += [{"role": "user", "content": "y" * 200}] * 12
    assert _aging.age_observations(convo, force=True, keep_chars=100, dry=True) > 15000
    assert all(m["content"] == big for m in convo[2:9:2])             # dry changed nothing
    st = SimpleNamespace(convo=convo, batch_unread=False, batch_mark=None)
    monkeypatch.setattr("aiforge_core.runtime.chat_agent._context._window._ctx_budget_chars",
                        lambda *a, **k: 20000)
    monkeypatch.setattr(_limits, "_PRUNE_MIN_SHARE", 5.0)              # never enough
    assert _limits._prune_at_condense_point(st, "chat") == 0
    assert all(m["content"] == big for m in convo[2:9:2])


def test_probe_guard_speaks_every_ten_shell_commands_without_an_edit(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _probe_guard as P
    # (a write under a temp folder is not a change to the work: not /tmp here)
    st = SimpleNamespace(edits_made=0, cwd="/srv/work")
    said = [P.note(st, "run_command", {"cmd": "ls -la shopkit"}) for _ in range(20)]
    assert [i for i, s in enumerate(said) if s] == [9, 19]
    P.note(st, "multi_edit", {"edits": []})            # refused: no landed edit
    assert st.shell_streak == 20
    st.edits_made = 1                                   # an edit landed
    P.note(st, "file_write", {"path": "a.py"})
    assert st.shell_streak == 0
    P.note(st, "run_command", {"cmd": "sed -i 's/a/b/' x.py"})   # a shell write
    assert st.shell_streak == 0
    ro = SimpleNamespace(edits_made=0, cwd="/tmp", readonly_mode=True)
    assert not any(P.note(ro, "run_command", {"cmd": "ls"}) for _ in range(12))
    monkeypatch.setenv("AIFORGE_CHAT_PROBE_GUARD", "0")
    st2 = SimpleNamespace(edits_made=0, cwd="/tmp")
    assert not any(P.note(st2, "run_command", {"cmd": "ls"}) for _ in range(12))


def test_a_top_level_multi_edit_path_is_seen_by_the_guards():
    from aiforge_core.runtime import scope_guard
    args = {"path": "/elsewhere/x.py", "edits": [{"old_str": "a", "new_str": "b"},
                                                  {"path": "own.py", "old_str": "c", "new_str": "d"}]}
    assert scope_guard._multi_edit_paths(args) == ["/elsewhere/x.py", "own.py"]
    assert scope_guard._multi_edit_paths({"file_path": "f.py", "edits": [{"old_str": "a"}]}) == ["f.py"]


def test_multi_edit_takes_one_path_for_all_edits(tmp_path):
    from aiforge_core.runtime.chat_agent._tools import _skills
    f = tmp_path / "m.py"
    f.write_text("a = 1\nb = 2\n")
    res = _skills._t_multi_edit({"path": "m.py", "edits": [
        {"old_text": "a = 1", "new_text": "a = 10"},
        {"old_str": "b = 2", "new_str": "b = 20"}]}, str(tmp_path))
    assert res.get("ok"), res
    assert f.read_text() == "a = 10\nb = 20\n"


def test_a_clean_spec_check_stands_in_for_the_model_done_check(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _done_check as D
    monkeypatch.setattr(D, "enabled", lambda: True)
    monkeypatch.setattr(D, "job_note_holds", lambda st: False)
    st = SimpleNamespace(session_id=1, contract_checked=5, contract_gaps=[])
    assert not D._applies(st, None, False)                 # proven by the code
    st.contract_gaps = ["X.y is missing"]
    assert D._applies(st, None, False)
    st2 = SimpleNamespace(session_id=1, done_check_calls=D._CALLS_PER_TURN)
    assert not D._applies(st2, None, False)                # bounded per turn


def test_malformed_tool_args_come_back_as_that_tools_error():
    from aiforge_core.runtime.chat_agent._turn import _action
    st = SimpleNamespace(convo=[])
    gen = _action._pre_dispatch_gates(st, "file_write", {"_invalid_args": "12 chars, ends: '{\"path\": \"a'"},
                                      False, False)
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        assert stop.value == "handled"
    assert events[0]["result"]["error"] == "invalid_arguments"
    assert "valid JSON" in st.convo[-1]["content"]


def test_probe_guard_counts_python_probes_across_edits():
    from aiforge_core.runtime.chat_agent._turn import _probe_guard as P
    st = SimpleNamespace(edits_made=0, cwd="/srv/work")
    said = []
    for i in range(16):
        said.append(P.note(st, "run_command", {"cmd": 'cd x && python3 -c "import a; print(hasattr(a, \'b\'))"'}))
        st.edits_made += 1                      # an edit between every probe
        P.note(st, "file_write", {"path": "a.py"})
    assert [i for i, s in enumerate(said) if s] == [7, 15]
    assert "grep tool" in said[7]
    assert not P.note(st, "run_command", {"cmd": "python3 -m pytest -q"})   # tests are not probes


def test_a_prune_also_cuts_the_spent_args_of_old_calls():
    """Old scripts that ran and edit text that landed are cut to a head at
    the condense point; recent calls and reads stay whole; JSON stays valid."""
    import json as _json
    from aiforge_core.runtime.chat_agent._context import _aging
    script = "python3 - <<'EOF'\n" + "print('x')\n" * 400 + "EOF"
    old = {"role": "assistant", "content": "ACTION: run_command\nARGS_JSON: " + _json.dumps({"cmd": script})}
    convo = [{"role": "system", "content": "s"}, old,
             {"role": "user", "content": "OBSERVATION: exit 0"}]
    convo += [{"role": "user", "content": "y" * 100}] * 12
    recent = {"role": "assistant", "content": "ACTION: run_command\nARGS_JSON: " + _json.dumps({"cmd": script})}
    convo.append(recent)
    freed = _aging.age_observations(convo, force=True, keep_chars=100, keep_msgs=1, dry=True)
    assert freed > 3000 and convo[1] is old                       # dry changed nothing
    _aging.age_observations(convo, force=True, keep_chars=100, keep_msgs=1)
    a = _json.loads(convo[1]["content"].split("ARGS_JSON: ", 1)[1])
    assert len(a["cmd"]) < 300 and "already ran/applied" in a["cmd"]
    assert convo[-1] is recent                                     # newest untouched


def test_a_deep_prune_is_considered_below_the_condense_line(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _limits
    from aiforge_core.runtime.chat_agent._context import _aging
    big = "OBSERVATION: " + "x" * 6000
    convo = [{"role": "system", "content": "s"}]
    for i in range(4):
        convo += [{"role": "assistant", "content": 'ACTION: file_read\nARGS_JSON: {"path": "a%d"}' % i},
                  {"role": "user", "content": big}]
    convo += [{"role": "user", "content": "y" * 100}] * 12
    st = SimpleNamespace(convo=convo, batch_unread=False, batch_mark=None)
    hist = sum(len(m["content"]) for m in convo[1:])
    monkeypatch.setattr("aiforge_core.runtime.chat_agent._context._window._ctx_budget_chars",
                        lambda *a, **k: int(hist / 0.8))                  # 80% full: under the line
    assert _limits._prune_at_condense_point(st, "chat") > 0                 # above the 70% soft line
    monkeypatch.setenv("AIFORGE_CTX_PRUNE_AT", "1.0")
    st2 = SimpleNamespace(convo=[dict(m) for m in convo], batch_unread=False, batch_mark=None)
    for m in st2.convo:
        if m["content"].startswith("OBSERVATION: [aged"): m["content"] = big
    assert _limits._prune_at_condense_point(st2, "chat") == 0              # only at the line


# click af5: ShellComplete lived in _shell_base.py, shell_completion never
# imported it, every hidden test failed to import (0/37). The request wrote it
# as a dotted CALL, which the parser dropped.

def test_a_dotted_call_is_an_export_of_that_module(tmp_path):
    pkg = tmp_path / "src" / "click"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "_shell_base.py").write_text("class ShellComplete:\n    pass\n")
    (pkg / "shell_completion.py").write_text("def shell_complete():\n    pass\n")
    req = "`click.shell_completion.ShellComplete(cli, prog_name, complete_var)` is the base"
    gaps = S.missing(req, str(tmp_path))
    assert any("ShellComplete" in g and "not defined" in g for g in gaps)
    (pkg / "shell_completion.py").write_text("from ._shell_base import ShellComplete\n")
    assert S.missing(req, str(tmp_path)) == []


def test_a_library_dotted_call_is_not_a_claim(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    assert S.missing("`os.path.join(a, b)` and `json.dumps(x)`", str(tmp_path)) == []


def test_the_progress_note_after_a_condense_says_each_gap_list_once(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    st = SimpleNamespace(goal="Add `Inventory.receive(sku, qty)` to `m.py`.",
                         cwd=str(tmp_path), convo=[], edits_made=1, steers=[])
    (tmp_path / "m.py").write_text("class Inventory:\n    pass\n")
    note = G.progress_note(st)
    assert note and "Inventory.receive" in note
    assert G.progress_note(st) is None             # same list: said once
    st.edits_made = 0
    st.contract_progress_said = None
    assert G.progress_note(st) is None             # nothing built yet


def test_a_src_layout_file_and_an_example_helper_are_not_gaps(tmp_path):
    pkg = tmp_path / "src" / "click"
    pkg.mkdir(parents=True)
    (pkg / "types.py").write_text("x = 1\n")
    req = ("Add it to `ParamType` (in\n   `click/types.py`): returns `[]`.\n"
           "Examples (with `_get_words(cli, args, incomplete)` returning values):")
    assert S.missing(req, str(tmp_path)) == []


def test_a_name_described_as_current_then_marked_for_removal_is_not_claimed(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    req = ("Its current support lives in `src/click/_bashcomplete.py` and is\n"
           "wired through `click.core._bashcomplete()`. Replace it with a new one.\n"
           "Files: `src/click/_bashcomplete.py` (remove or\nreplace).")
    assert S.claims(req) == []


# review of the dotted-call change

def test_a_lowercase_call_on_a_library_object_is_no_gap(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    assert S.missing("Load it with `app.config.from_object(Config)`.", str(tmp_path)) == []


def test_a_name_defined_in_a_try_block_is_exported(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "compat.py").write_text(
        "try:\n    from ._speedups import fast_parse\nexcept ImportError:\n"
        "    def fast_parse(text):\n        return text\n")
    assert S.missing("Use `pkg.compat.fast_parse(text)`.", str(tmp_path)) == []


def test_a_missing_file_is_reported_when_the_request_names_only_files(tmp_path):
    (tmp_path / "shopkit").mkdir()
    (tmp_path / "shopkit" / "core.py").write_text("x = 1\n")
    assert S.missing("Create `shopkit/invoices.py` with the invoice logic.",
                     str(tmp_path)) == ["shopkit/invoices.py does not exist"]


def test_extending_an_existing_class_still_claims_the_new_signature():
    cs = S.claims("Extend the existing `Inventory` class with "
                  "`Inventory.receive(sku, qty, *, expiry=None)`.")
    assert any(c.get("name") == "receive" for c in cs)


def test_a_heading_ends_the_current_state_sentence():
    cs = S.claims("## Currently it is in `parse.py`\n\n## Required\n- `pkg.cli.Main(args)`")
    assert any(c.get("name") == "Main" for c in cs)


def test_the_target_of_a_rename_move_or_replace_is_still_claimed():
    texts = {c["text"] for c in S.claims(
        "Rename `load(path)` to `load_config(path, *, strict=True)`. "
        "Move the helpers into `pkg/helpers.py`. "
        "Add `src/click/shell_completion.py`, which replaces `src/click/_bashcomplete.py`.")}
    assert "load_config(path, *, strict=True)" in texts
    assert "pkg/helpers.py" in texts and "src/click/shell_completion.py" in texts
    assert "src/click/_bashcomplete.py" not in texts and "load(path)" not in texts


def test_an_example_mention_does_not_hide_a_later_requirement():
    cs = S.claims("For example, `cache.get(key, default=None)` returns it.\n\n"
                  "Required: `Cache.get(key, default=None)`.")
    assert any(c.get("cls") == "Cache" for c in cs)


def test_an_input_output_arrow_pair_is_not_claimed():
    assert S.claims("upper-cased as today\n(`log_changes()` -> `LOG_CHANGES()`).") == []


# second review: gone rules read the text between spans

def _texts(req):
    return {c["text"] for c in S.claims(req)}


def test_removal_chains_and_noun_phrases_stay_gone():
    t = _texts("Remove `Cart.old_total()` along with `Cart.legacy_tax()`. "
               "Remove calls to `Loader.load_legacy()`.")
    assert t == set()


def test_passive_and_bullet_forms_after_a_name_are_gone():
    t = _texts("`old_a()` and `old_b()` are removed.\n- `old_c()`: remove\n"
               "`Cls.x` was renamed to `Cls.y(a, *, b=1)`. `Mod.Z` needs to be deleted.")
    assert t == {"Cls.y(a, *, b=1)"}


def test_names_that_contain_or_describe_a_verb_are_claimed():
    t = _texts("Add `Inventory.remove(sku)` and `Inventory.restock(sku, qty=1)`. "
               "`prune(days=30)` deletes old rows; `new.py` replaces the old file.")
    assert {"Inventory.remove(sku)", "Inventory.restock(sku, qty=1)", "prune(days=30)"} <= t


def test_the_new_name_beside_a_removed_one_is_claimed():
    t = _texts("Instead of `old_fn(x)`, use `new_fn(x, *, y=1)`. "
               "Remove `old_api()` and add `new_api(x, y=1)`.")
    assert t == {"new_fn(x, *, y=1)", "new_api(x, y=1)"}


def test_a_current_state_clause_followed_by_an_ask_is_claimed():
    t = _texts("Currently there is no export; add `export_csv(path, *, sep=',')`. "
               "Since `Cache.get` currently is unbounded, add `Cache.evict(n=10)`.")
    assert {"export_csv(path, *, sep=',')", "Cache.evict(n=10)"} <= t


def test_a_signature_arrow_return_is_a_claim():
    assert "parse(text: str)" in _texts("`parse(text: str)` -> `Tree`")


def test_a_type_checking_import_is_not_an_export(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
                                "    from .other import Name\n")
    assert S.missing("Use `pkg.mod.Name`.", str(tmp_path)) == ["pkg.mod.Name is not defined"]


def test_a_same_named_file_deep_in_fixtures_does_not_count(tmp_path):
    (tmp_path / "tests" / "fixtures" / "shopkit").mkdir(parents=True)
    (tmp_path / "tests" / "fixtures" / "shopkit" / "invoices.py").write_text("")
    (tmp_path / "core.py").write_text("x = 1\n")
    assert S.missing("Create `shopkit/invoices.py`.", str(tmp_path)) == [
        "shopkit/invoices.py does not exist"]



# third review: conservative gone rules + the waiver

def test_three_names_before_are_removed_are_all_gone():
    assert _texts("`old_a()`, `old_b()` and `old_c()` are removed.") == set()


def test_a_colon_list_and_ie_after_a_removal_verb_are_gone():
    assert _texts("Remove the old helpers: `old_a()`, `old_b()`.") == set()
    assert _texts("Remove the v1 client methods, i.e. `Client.get_v1()` and "
                  "`Client.post_v1()`.") == set()


def test_rename_pairs_drop_every_source():
    assert _texts("Rename `get()` to `fetch(key, *, ttl=None)` and `put()` to "
                  "`store(key, value, *, ttl=None)`.") == {
        "fetch(key, *, ttl=None)", "store(key, value, *, ttl=None)"}


def test_a_new_api_after_a_removal_is_claimed():
    t = _texts("Remove the global cache and route lookups through "
               "`Registry.lookup(name, *, default=None)`. Do not remove "
               "`Cache.get(key, default=None)`. Add a delete endpoint "
               "`Api.delete_item(id, *, soft=False)`. `new_api(x, y=1)`, renamed from `old_api`.")
    assert {"Registry.lookup(name, *, default=None)", "Cache.get(key, default=None)",
            "Api.delete_item(id, *, soft=False)", "new_api(x, y=1)"} <= t


def test_a_current_state_list_after_a_colon_is_not_claimed():
    assert _texts("Its current implementation lives in two files: `cli/old.py` and "
                  "`cli/compat.py`. Merge them into `cli/main.py`.") == {"cli/main.py"}


def test_a_package_relative_file_is_found_deep_in_src(tmp_path):
    d = tmp_path / "src" / "mytool" / "commands"
    d.mkdir(parents=True)
    (d / "export.py").write_text("")
    assert S.missing("Add `commands/export.py`.", str(tmp_path)) == []


def test_the_else_of_a_type_checking_guard_is_exported(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
                                "    from ._t import Row\nelse:\n    Row = dict\n")
    assert S.missing("Use `pkg.mod.Row`.", str(tmp_path)) == []


def test_an_example_call_with_literal_args_is_not_claimed():
    assert _texts("`parse('a=1')` -> `{'a': 1}`") == set()


def _drain(g):
    try:
        while True:
            next(g)
    except StopIteration as e:
        return e.value


def test_the_model_can_waive_a_wrong_gap(tmp_path):
    (tmp_path / "m.py").write_text("class Cart:\n    pass\n")
    st = SimpleNamespace(goal="Add `Cart.total(self, *, tax=0)`.", cwd=str(tmp_path),
                         convo=[], edits_made=1, steers=[])
    assert _drain(G.gate(st, {"text": "done"})) == "continue"
    assert "Cart.total" in st.contract_gaps[0]
    _drain(G.gate(st, {"text": "Done.\nnot required: Cart.total"}))
    assert st.contract_gaps == []


# fourth review

def test_a_padded_table_row_does_not_hang_the_gone_rules():
    import time
    t0 = time.monotonic()
    S.claims("| `old_total()` | remove" + " " * 40 + "| `new_total()` |")
    assert time.monotonic() - t0 < 1


def test_a_purpose_to_after_a_new_name_keeps_it_claimed():
    assert "new_b(y, *, z=1)" in _texts(
        "Replace `old()` with `new_a(x, *, z=1)` and `new_b(y, *, z=1)` to support it.")


def test_a_waiver_must_name_a_gap_exactly(tmp_path):
    (tmp_path / "m.py").write_text("class Cache:\n    pass\n")
    st = SimpleNamespace(goal="Add `Cache.get_all(self, *, n=1)`.", cwd=str(tmp_path),
                         convo=[], edits_made=1, steers=[])
    for prose in ("is it not required: no, it is", "not required: get", "not required: a"):
        _drain(G.gate(st, {"text": prose}))
        assert st.contract_gaps, prose
    _drain(G.gate(st, {"text": "not required: Cache.get_all"}))
    assert st.contract_gaps == [] and st.contract_checked == 0


def test_a_tests_path_the_request_names_is_found_deeper(tmp_path):
    d = tmp_path / "backend" / "tests"
    d.mkdir(parents=True)
    (d / "test_export.py").write_text("")
    assert S.missing("Add `tests/test_export.py`.", str(tmp_path)) == []


def test_not_type_checking_body_is_exported_and_its_else_is_not(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("from typing import TYPE_CHECKING\nif not TYPE_CHECKING:\n"
                                "    A = dict\nelse:\n    from ._t import B\n")
    assert S.missing("Use `pkg.mod.A` and `pkg.mod.B`.", str(tmp_path)) == [
        "pkg.mod.B is not defined"]


def test_probe_guard_forces_a_write_step_after_two_ignored_notes(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _probe_guard as P
    from aiforge_core.runtime.chat_agent import _native
    st = SimpleNamespace(edits_made=1, probe_edits_seen=1, cwd="/srv/work", session_id=5)
    said = [P.note(st, "run_command", {"cmd": "ls -la shopkit"}) for _ in range(20)]
    assert "can only edit files" in said[19] and 5 in _native._FORCE_WRITE
    _native._FORCE_WRITE.discard(5)
    assert st.shell_streak == 0
    # no edit yet this turn (an ops run): notes only, never forced
    st2 = SimpleNamespace(edits_made=0, cwd="/srv/work", session_id=6)
    said = [P.note(st2, "run_command", {"cmd": "ls"}) for _ in range(20)]
    assert 6 not in _native._FORCE_WRITE and "can only edit" not in said[19]
