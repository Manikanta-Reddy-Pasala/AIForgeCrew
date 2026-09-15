"""Host paths, box paths, and which folders may be handed to the agent."""

from __future__ import annotations

from aiforge_cli import paths


def test_posix_paths_are_the_same_inside_the_box():
    assert paths.to_box("/Users/m/work", platform="posix") == "/Users/m/work"
    assert paths.to_host("/Users/m/work", platform="posix") == "/Users/m/work"


def test_windows_paths_map_under_host_and_back():
    box = paths.to_box(r"C:\Users\m\work", platform="nt")
    assert box == "/host/c/Users/m/work"
    assert paths.to_host(box, platform="nt") == r"C:\Users\m\work"


def test_windows_round_trip_keeps_nested_folders():
    original = r"D:\src\PosServerBackend\src\main"
    assert paths.to_host(paths.to_box(original, platform="nt"), platform="nt") == original


def test_normalize_expands_home_and_strips_trailing_slash():
    assert paths.normalize_host("~/work/", home="/Users/m", platform="posix") == "/Users/m/work"
    assert paths.normalize_host("rel", cwd="/Users/m", platform="posix") == "/Users/m/rel"


def test_covering_mount_prefers_the_longest_match():
    assert paths.covering_mount("/a/b/c", ["/a", "/a/b"], platform="posix") == "/a/b"
    assert paths.covering_mount("/other", ["/a"], platform="posix") is None


def test_a_sibling_prefix_is_not_a_mount():
    # /a/bc must not count as inside /a/b — a string prefix is not a path prefix.
    assert paths.covering_mount("/a/bc", ["/a/b"], platform="posix") is None


def test_home_and_root_are_refused(tmp_path):
    assert "too broad" in paths.mount_refusal("/", home=str(tmp_path), platform="posix")
    assert "too broad" in paths.mount_refusal(str(tmp_path), home=str(tmp_path / "x"),
                                              platform="posix")


def test_a_real_folder_is_allowed(tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    assert paths.mount_refusal(str(folder), home=str(tmp_path / "home"),
                               platform="posix") is None


def test_a_path_with_a_compose_metacharacter_is_refused(tmp_path):
    assert "without" in paths.mount_refusal("/work:/etc", home=str(tmp_path),
                                            platform="posix")


def test_the_approvals_directory_can_never_be_mounted(tmp_path):
    # ~/.config/aiforge holds approved-mounts. Mounting it would let the box
    # approve its own future mounts — a one-way door out of the sandbox.
    home = tmp_path / "home"
    (home / ".config" / "aiforge").mkdir(parents=True)
    why = paths.mount_refusal(str(home / ".config" / "aiforge"), home=str(home),
                              platform="posix")
    assert why is not None and "approve its own mounts" in why


def test_credential_directories_are_refused(tmp_path):
    home = tmp_path / "home"
    for name in (".ssh", ".aws", ".kube"):
        (home / name).mkdir(parents=True)
        why = paths.mount_refusal(str(home / name), home=str(home), platform="posix")
        assert why is not None and "credentials" in why


def test_system_directories_are_refused(tmp_path):
    assert "system files" in paths.mount_refusal("/etc/nginx", home=str(tmp_path),
                                                 platform="posix")


def test_a_project_folder_next_to_a_guarded_one_is_still_allowed(tmp_path):
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    assert paths.mount_refusal(str(home / "work"), home=str(home),
                               platform="posix") is None


def test_a_dotdot_path_cannot_walk_into_a_guarded_directory(tmp_path):
    # ~/work/../.ssh used to slip past every guard as a string while the
    # filesystem resolved it perfectly well.
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / "work").mkdir()
    walked = paths.normalize_host("../.ssh", cwd=str(home / "work"), home=str(home),
                                  platform="posix")
    assert walked == str(home / ".ssh")
    assert paths.mount_refusal(walked, home=str(home), platform="posix") is not None


def test_the_parent_of_home_is_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    parent = paths.normalize_host(str(home / ".."), home=str(home), platform="posix")
    assert paths.mount_refusal(parent, home=str(home), platform="posix") is not None


def test_case_is_folded_where_the_filesystem_folds_it(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    why = paths.mount_refusal(str(home / ".SSH"), home=str(home), platform="darwin")
    assert why is not None and "credentials" in why


def test_a_symlink_into_a_guarded_directory_is_refused(tmp_path):
    import os
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / "work").mkdir()
    link = home / "work" / "keys"
    os.symlink(home / ".ssh", link)
    # docker resolves the link when it binds, so the guard has to as well.
    why = paths.mount_refusal(str(link), home=str(home), platform="posix")
    assert why is not None and "credentials" in why


def test_a_folder_that_CONTAINS_a_guarded_directory_is_refused(tmp_path):
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    why = paths.mount_refusal(str(home), home=str(home), platform="posix")
    assert why is not None


def test_var_is_refused_because_var_run_is(tmp_path):
    # /var/run/docker.sock inside the box is a way out of it.
    assert paths.mount_refusal("/var", home=str(tmp_path), platform="posix") is not None


def test_a_windows_drive_relative_path_is_not_treated_as_absolute():
    assert "absolute" in paths.mount_refusal(r"C:work", home=r"C:\Users\m",
                                             platform="nt")


def test_a_newline_in_a_path_is_refused(tmp_path):
    assert "newline" in paths.mount_refusal("/work/we\nird", home=str(tmp_path),
                                            platform="posix")
