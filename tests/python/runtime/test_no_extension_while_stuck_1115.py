"""A turn the stuck guards keep stepping into earns no extension.

Live case: a real PosClientBackend turn patched files back and forth, had 45
duplicate reads skipped, 12 discard commands refused and 3 changes of
approach; every landed edit counted as progress, so its 40-minute turn was
extended and ran 62+ minutes in a loop.
"""
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _limits


def _st(**kw):
    base = dict(granted_at_step=None, extensions_used=0, ext_budget=2, wt_fp0=False,
                cwd=".", reads_new=5, edits_made=3, progress_mark=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_progress_without_spin_extends():
    st = _st()
    assert _limits._may_extend(st, 10) is True and st.extensions_used == 1


def test_spinning_turn_is_not_extended():
    st = _st()
    for _ in range(_limits._MAX_SPIN):
        _limits.note_spin(st)
    assert _limits._may_extend(st, 10) is False and st.extensions_used == 0


def test_spin_counts_from_the_last_grant(monkeypatch):
    st = _st()
    for _ in range(_limits._MAX_SPIN - 1):
        _limits.note_spin(st)
    assert _limits._may_extend(st, 10) is True        # under the cap: granted
    st.edits_made += 1                                # new work after the grant
    _limits.note_spin(st)                             # only one new intervention
    assert _limits._may_extend(st, 20) is True


def test_off_switch(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_EXTEND_MAX_SPIN", "0")
    st = _st()
    for _ in range(50):
        _limits.note_spin(st)
    assert _limits._may_extend(st, 10) is True
