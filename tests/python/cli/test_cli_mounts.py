"""A line in mounts.list is a request; only this host grants it."""

from __future__ import annotations

from aiforge_cli import mounts


def _files(tmp_path):
    return tmp_path / "mounts.list", tmp_path / "approved-mounts"


def test_a_requested_folder_is_not_mounted_until_it_is_approved(tmp_path):
    listed, approved = _files(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    # The box itself can write this file, so a line in it grants nothing.
    listed.write_text(f"{work}\n")
    assert mounts.requested(listed) == [str(work)]
    assert mounts.effective(listed, approved, home=str(tmp_path / "home")) == []
    assert mounts.pending(listed, approved) == [str(work)]

    mounts.add(listed, approved, str(work), approve=True)
    assert mounts.effective(listed, approved, home=str(tmp_path / "home")) == [str(work)]
    assert mounts.pending(listed, approved) == []


def test_adding_without_approval_records_the_request_only(tmp_path):
    listed, approved = _files(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    mounts.add(listed, approved, str(work), approve=False)
    assert mounts.requested(listed) == [str(work)]
    assert mounts.approved(approved) == []


def test_an_approved_folder_that_no_longer_exists_is_dropped(tmp_path):
    listed, approved = _files(tmp_path)
    mounts.add(listed, approved, str(tmp_path / "gone"), approve=True)
    assert mounts.effective(listed, approved, home=str(tmp_path / "home")) == []


def test_removing_clears_both_the_request_and_the_approval(tmp_path):
    listed, approved = _files(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    mounts.add(listed, approved, str(work), approve=True)
    mounts.remove(listed, approved, str(work))
    assert mounts.requested(listed) == []
    assert mounts.approved(approved) == []


def test_comments_blank_lines_and_duplicates_are_ignored(tmp_path):
    listed, _ = _files(tmp_path)
    listed.write_text("# a note\n\n/a\n/a\n  /b  \n")
    assert mounts.requested(listed) == ["/a", "/b"]


def test_approvals_are_written_owner_only(tmp_path):
    listed, approved = _files(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    mounts.add(listed, approved, str(work), approve=True)
    assert oct(approved.stat().st_mode)[-3:] == "600"


def test_an_approval_for_the_approvals_directory_still_cannot_take_effect(tmp_path):
    # However the line got there, the intersection re-validates it.
    listed, approved = _files(tmp_path)
    approvals_dir = tmp_path / "approvals"
    approvals_dir.mkdir()
    approved_file = approvals_dir / "approved-mounts"
    listed.write_text(f"{approvals_dir}\n")
    approved_file.write_text(f"{approvals_dir}\n")
    assert mounts.effective(listed, approved_file, home=str(tmp_path / "home")) == []
