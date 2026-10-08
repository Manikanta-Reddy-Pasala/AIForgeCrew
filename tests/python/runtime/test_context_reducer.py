"""The reducers share one interface; they behave as the functions they wrap."""
import pytest

from aiforge_core.runtime.chat_agent._context import _aging as A
from aiforge_core.runtime.chat_agent._context import _compaction as C
from aiforge_core.runtime.chat_agent._context import reducer as R


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_AGE_BURST", "1")


def _pair(tool, body):
    return [{"role": "assistant", "content": f"ACTION: {tool}\nARGS_JSON: {{}}"},
            {"role": "user", "content": "OBSERVATION: " + body}]


def _convo(body="x" * 6000, tail=12):
    msgs = [{"role": "system", "content": "s"}] + _pair("file_read", body)
    for _ in range(tail // 2):
        msgs += _pair("run_command", "ok")
    return msgs


def test_age_old_is_the_aging_function(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_AGE_OBS", "1")      # per-step ageing is opt-in
    a, b = _convo(), _convo()
    assert A.age_observations(a) == 1
    assert R.AgeOld().reduce(b) is b
    assert a == b and "[aged:" in b[2]["content"]


def test_age_old_passes_protect_and_forget_through(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_AGE_OBS", "1")      # per-step ageing is opt-in
    convo = _convo()
    forget = {"file_read|{}"}
    R.AgeOld(protect_from=1, forget=forget).reduce(convo)
    assert "[aged:" not in convo[2]["content"]            # protected
    R.AgeOld(forget=forget).reduce(convo)
    assert "[aged:" in convo[2]["content"] and not forget  # aged and forgotten


def test_fits_counts_history_only():
    convo = [{"role": "system", "content": "s" * 999}, {"role": "user", "content": "abcd"}]
    assert R.history_chars(convo) == 4
    assert R.fits(convo, 4) and not R.fits(convo, 3) and R.fits(convo, 0)


def test_condense_is_compact_convo(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_CONTEXT_BUDGET_CHARS", "2000")
    convo = [{"role": "system", "content": "s"}]
    for i in range(30):
        convo += [{"role": "user", "content": f"ask {i} " + "y" * 400},
                  {"role": "assistant", "content": "FINAL: ok " + "z" * 400}]
    want = C._compact_convo(list(convo), role="doer", force=True)
    red = R.Condense(role="doer", force=True)
    assert red.applies(convo, 10**9)                       # force beats a fit
    assert red.reduce(list(convo)) == want
    assert not R.Condense(role="doer").applies(convo, 10**9)
    assert R.Condense(role="doer").applies(convo, 100)


class _Spy:
    def __init__(self, name, shrink, applies=True):
        self.name, self.shrink, self._applies, self.ran = name, shrink, applies, 0

    def applies(self, convo, budget):
        return self._applies

    def reduce(self, convo, budget):
        self.ran += 1
        return convo[:len(convo) - self.shrink]


def _msgs(n, size=10):
    return [{"role": "system", "content": "s"}] + [
        {"role": "user", "content": "u" * size} for _ in range(n)]


def test_reduce_to_budget_runs_cheapest_first_and_stops_when_it_fits():
    cheap, dear = _Spy("cheap", 3), _Spy("dear", 5)
    out = R.reduce_to_budget(_msgs(10), 80, [cheap, dear])   # 100 -> 70 fits
    assert (cheap.ran, dear.ran) == (1, 0) and R.history_chars(out) == 70


def test_reduce_to_budget_goes_on_and_skips_what_does_not_apply():
    skip, a, b = _Spy("skip", 9, applies=False), _Spy("a", 1), _Spy("b", 5)
    out = R.reduce_to_budget(_msgs(10), 40, [skip, a, b])
    assert (skip.ran, a.ran, b.ran) == (0, 1, 1) and R.history_chars(out) == 40


def test_reduce_to_budget_untouched_when_already_fits():
    spy = _Spy("a", 1)
    convo = _msgs(3)
    assert R.reduce_to_budget(convo, 0, [spy]) is convo and spy.ran == 0


def test_condense_block_is_the_one_sentinel_form():
    assert C.condense_block("x") == "<<AIFORGE_CTX_CONDENSED>>\nx\n<</AIFORGE_CTX_CONDENSED>>"
    assert C._prior_block(C.condense_block("hello")) == "\nhello\n"
