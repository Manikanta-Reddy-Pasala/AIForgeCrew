"""Destructive-delete detection — truncate -s, dd of=<file>, cp /dev/null.

NOTE: plain `>` output redirection is NOT a delete (round-4 regression fix):
it's creation/overwrite, and the autonomous Doer has no approval path, so
flagging `cmd > file` would hard-fail every redirect."""
import pytest

from aiforge_core.runtime.tools import delete_guard as dg


# ── existing destructive forms still trip ────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "rm -rf /tmp/x",
    "rmdir foo",
    "git reset --hard HEAD~1",
    "drop table users",
    "truncate table foo",
    "mkfs.ext4 /dev/sdb",
])
def test_existing_destructive_still_caught(cmd):
    assert dg.is_destructive_delete(cmd) is True


# ── round-3 new destructive forms ────────────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "truncate -s 0 data.txt",           # coreutils truncate
    "truncate --size 0 data.txt",
    "dd if=/dev/zero of=/dev/sda",      # raw disk
    "dd if=in.iso of=out.img bs=4M",    # file overwrite
    "cp /dev/null logfile",             # truncate via cp
])
def test_new_destructive_forms_caught(cmd):
    assert dg.is_destructive_delete(cmd) is True


# ── benign redirections / fd-dups MUST NOT trip ──────────────────────────────
# Plain `>` output redirection is creation/overwrite, NOT a delete.
@pytest.mark.parametrize("cmd", [
    "cat a >> b.txt",                   # append
    "cmd > /dev/null",                  # discard sink
    "cmd >/dev/null 2>&1",
    "build 2>&1 | tee out",             # fd-dup
    "cmd 2> err.txt",                   # stderr redirect (digit-prefixed)
    "echo x >& 2",                      # fd-dup
    "dd if=a of=/dev/null",             # harmless dd sink
    "cat x > out.txt",                  # ordinary output redirection
    "npm run build > log 2>&1",         # build log
    "echo hi > file.txt",
    "echo > .env",                      # create/overwrite a file
    "cmd >out.txt",                     # no space
    "ls -la",
    "python app.py",
    "npm run dev",
    "",
])
def test_benign_not_caught(cmd):
    assert dg.is_destructive_delete(cmd) is False


def test_allow_delete_env(monkeypatch):
    monkeypatch.delenv("AIFORGE_ALLOW_DELETE", raising=False)
    assert dg.allow_delete() is False
    monkeypatch.setenv("AIFORGE_ALLOW_DELETE", "1")
    assert dg.allow_delete() is True
