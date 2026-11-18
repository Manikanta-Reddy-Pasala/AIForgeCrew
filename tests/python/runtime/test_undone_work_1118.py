"""Edits all undone again count as no change; a flagged test is restored once."""
import subprocess
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._guards import zero_edit as ze


def _repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (r / "a.kt").write_text("x\n")
    g("add", "."); g("commit", "-qm", "base")
    return r


def _guard(r):
    return ze.ZeroEditGuard(str(r), readonly_mode=False, builder=False, plan_mode=False,
                            asks=None, wt_fp0=ze._worktree_fingerprint(str(r))) \
        if hasattr(ze, "ZeroEditGuard") else None


def test_edits_undone_on_disk_still_count_as_no_change(tmp_path):
    r = _repo(tmp_path)
    cls = [c for c in vars(ze).values() if isinstance(c, type) and hasattr(c, "applies") and hasattr(c, "evidence")][0]
    g = cls.__new__(cls)
    g.cwd, g.readonly_mode, g.builder, g.plan_mode, g.asks, g.strict = str(r), False, False, False, None, False
    g.wt_fp0 = ze._worktree_fingerprint(str(r))
    head = ze.head_commit(str(r))
    st = SimpleNamespace(edits_made=3, head0=head, session_id=None)
    assert g.applies(st)                                   # written then put back: no change on disk
    (r / "a.kt").write_text("y\n")
    assert not g.applies(st)                               # a real change
