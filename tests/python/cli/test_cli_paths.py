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
