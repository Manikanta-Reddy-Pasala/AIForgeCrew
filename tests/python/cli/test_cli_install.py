"""The one installer: the binary carries the sandbox source, `aiforge install`
puts itself on PATH and builds + starts the box (which serves the web UI)."""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest
from aiforge_cli import box, install, payload
from aiforge_cli.config import Config


def _tar(tmp_path: Path, files: dict[str, str], name="sandbox-src.tar.gz") -> Path:
    p = tmp_path / name
    with tarfile.open(p, "w:gz") as tar:
        for arc, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(arc)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return p


# ── the carried source ────────────────────────────────────────────────────

def test_the_source_unpacks_once_per_content_and_names_the_image(tmp_path):
    src = _tar(tmp_path, {"Dockerfile": "FROM ubuntu:24.04\n", "run.sh": "echo\n"})
    root = tmp_path / "sandbox"
    first = payload.extract(root, src)
    assert (first / "Dockerfile").read_text().startswith("FROM")
    assert payload.extract(root, src) == first                    # reused, not re-unpacked
    assert payload.image_tag(src) == f"aiforge-sandbox:{first.name[4:]}"
    newer = _tar(tmp_path, {"Dockerfile": "FROM ubuntu:24.04\n# v2\n"}, "v2.tar.gz")
    second = payload.extract(root, newer)
    assert second != first and not first.exists()                 # the old unpack is cleared


def test_an_archive_escaping_its_folder_is_refused(tmp_path):
    bad = _tar(tmp_path, {"../evil": "x"})
    with pytest.raises(ValueError, match="outside"):
        payload.extract(tmp_path / "sandbox", bad)


def test_from_source_there_is_no_payload():
    assert payload.tarball() is None and payload.image_tag() is None


# ── the box from the carried source ───────────────────────────────────────

def _cfg(tmp_path, image) -> Config:
    return Config(port=8799, config_dir=tmp_path / ".aiforge", repo=None, image=image,
                  auto_mount=False, verbosity=0, json_events=False)


def test_a_missing_image_is_built_from_the_carried_source(tmp_path, monkeypatch):
    src = _tar(tmp_path, {"Dockerfile": "FROM ubuntu:24.04\n"})
    tag = payload.image_tag(src)
    ran: list = []
    monkeypatch.setattr(payload, "tarball", lambda: src)
    _fake_docker(monkeypatch, kind="native")
    monkeypatch.setattr(box, "_run_stream", lambda cmd, cwd=None: ran.append(cmd) or iter(()))
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path)}
    box.start(_cfg(tmp_path, tag), env=env)
    build, up = ran
    assert (tmp_path / ".aiforge").is_dir()                  # made by us, not by root-docker
    assert build[:4] == ["docker", "build", "-t", tag]
    assert "APP_UID=" + str(os.getuid()) in build and Path(build[-1]).name.startswith("src-")
    assert "APP_HOME=/home/aiforge" in build                 # where compose mounts ~/.aiforge
    assert up[:4] == ["docker", "compose", "-p", "aiforge"] and up[-2:] == ["up", "-d"]
    compose = Path(up[5]).read_text()
    assert "network_mode: host" in compose and "--host 127.0.0.1" in compose
    assert "ALLOW_UNAUTH" not in compose


def _fake_docker(monkeypatch, *, kind: str, present: bool = False) -> None:
    monkeypatch.setattr(box, "require_docker", lambda **kw: "docker")
    monkeypatch.setattr(box, "require_compose", lambda exe: None)
    monkeypatch.setattr(box, "daemon_kind", lambda exe, env=None: kind)
    monkeypatch.setattr(box, "_refuse_a_foreign_box", lambda exe, env: None)
    monkeypatch.setattr(box, "image_present", lambda exe, image: present)


def test_rootless_docker_builds_the_box_to_run_as_its_root(tmp_path, monkeypatch):
    """Rootless maps YOUR uid to root inside; any other uid is a subuid that
    cannot write your ~/.aiforge."""
    src = _tar(tmp_path, {"Dockerfile": "FROM ubuntu:24.04\n"})
    ran: list = []
    monkeypatch.setattr(payload, "tarball", lambda: src)
    _fake_docker(monkeypatch, kind="rootless")
    monkeypatch.setattr(box, "_run_stream", lambda cmd, cwd=None: ran.append(cmd) or iter(()))
    box.start(_cfg(tmp_path, payload.image_tag(src)),
              env={"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path)})
    build, up = ran
    assert "APP_UID=0" in build and "APP_GID=0" in build
    assert "'127.0.0.1:8799:8799'" in Path(up[5]).read_text()   # published, not host net


def test_an_overridden_image_is_named_not_blamed_on_the_binary(tmp_path, monkeypatch):
    src = _tar(tmp_path, {"Dockerfile": "FROM ubuntu:24.04\n"})
    monkeypatch.setattr(payload, "tarball", lambda: src)
    _fake_docker(monkeypatch, kind="native")
    with pytest.raises(box.BoxError, match="Unset it") as exc:
        box.start(_cfg(tmp_path, "corp/aiforge:1"), env={"HOME": str(tmp_path)})
    assert "download the aiforge binary" not in str(exc.value)


def test_compose_is_checked_before_a_minutes_long_build(tmp_path, monkeypatch):
    src = _tar(tmp_path, {"Dockerfile": "FROM ubuntu:24.04\n"})
    monkeypatch.setattr(payload, "tarball", lambda: src)
    _fake_docker(monkeypatch, kind="native")
    monkeypatch.setattr(box.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1})())
    monkeypatch.setattr(box, "require_compose", _real_compose)
    monkeypatch.setattr(box, "build_image", lambda *a, **k: pytest.fail("built first"))
    with pytest.raises(box.BoxError, match="docker-compose-v2"):
        box.start(_cfg(tmp_path, payload.image_tag(src)), env={"HOME": str(tmp_path)})


_real_compose = box.require_compose


def test_the_compose_project_is_a_name_compose_accepts():
    assert box.project_name({"AIFORGE_CONTAINER": "My.Box"}) == "my-box"
    assert box.project_name({}) == "aiforge"


def test_no_source_and_no_image_says_how_to_get_one(tmp_path, monkeypatch):
    monkeypatch.setattr(payload, "tarball", lambda: None)
    _fake_docker(monkeypatch, kind="native")
    with pytest.raises(box.BoxError, match="aiforge install"):
        box.start(_cfg(tmp_path, "aiforge-sandbox:local"), env={"HOME": str(tmp_path)})


def test_the_generated_box_can_bind_inside_its_container(tmp_path):
    """Docker Desktop / rootless: 0.0.0.0 inside the container, published on the
    host's 127.0.0.1 only — without the explicit flag the API's boot guard
    refuses and it crash-loops. The host's model server is host.docker.internal."""
    text = box.render_compose(_cfg(tmp_path, "img"), [], env={})
    assert "AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1" in text
    assert "'127.0.0.1:8799:8799'" in text
    assert "'host.docker.internal:host-gateway'" in text


def test_native_linux_shares_the_host_network_bound_to_loopback(tmp_path):
    """docker < 28 lets a LAN neighbour reach a container IP whatever address
    the port was published on — so on native Linux there is no container IP:
    host networking, the API on 127.0.0.1, no unauthenticated override."""
    text = box.render_compose(_cfg(tmp_path, "img"), [], env={}, host_net=True)
    assert "network_mode: host" in text and "--host 127.0.0.1 --port 8799" in text
    assert "ports:" not in text and "ALLOW_UNAUTH" not in text


@pytest.mark.parametrize("info, kind", [
    ("Ubuntu 24.04 LTS|[\"name=apparmor\",\"name=seccomp,profile=builtin\"]", "native"),
    ("Ubuntu 24.04 LTS|[\"name=seccomp\",\"name=rootless\"]", "rootless"),
    ("Docker Desktop|[\"name=seccomp\"]", "desktop"),
])
def test_the_daemon_kind_is_read_from_docker_info(monkeypatch, info, kind):
    monkeypatch.setattr(box._platform, "system", lambda: "Linux")
    monkeypatch.setattr(box, "_daemon_endpoint", lambda exe, env: "unix:///var/run/docker.sock")
    monkeypatch.setattr(box.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": info, "returncode": 0})())
    assert box.daemon_kind("docker", {}) == kind


def test_a_local_daemon_on_a_mac_is_in_a_vm_whatever_it_calls_itself(monkeypatch):
    """colima reports a plain Linux; on a Mac it is still a VM's daemon."""
    monkeypatch.setattr(box._platform, "system", lambda: "Darwin")
    monkeypatch.setattr(box, "_daemon_endpoint", lambda exe, env: "unix:///Users/m/.colima/docker.sock")
    monkeypatch.setattr(box.subprocess, "run", lambda *a, **k: type(
        "R", (), {"stdout": "Ubuntu 24.04 LTS|[]", "returncode": 0})())
    assert box.daemon_kind("docker", {}) == "desktop"


def test_a_failing_docker_info_on_linux_keeps_the_safe_host_network(monkeypatch):
    monkeypatch.setattr(box._platform, "system", lambda: "Linux")
    monkeypatch.setattr(box, "_daemon_endpoint", lambda exe, env: "")
    monkeypatch.setattr(box.subprocess, "run", lambda *a, **k: type(
        "R", (), {"stdout": "template error", "returncode": 1})())
    assert box.daemon_kind("docker", {}) == "native"


@pytest.mark.parametrize("host", ["ssh://me@server", "tcp://10.0.0.5:2376",
                                  "https://docker.corp:2376"])
def test_a_daemon_on_another_machine_is_refused(monkeypatch, host):
    """It would publish a token-less shell API on THAT machine."""
    monkeypatch.setattr(box._platform, "system", lambda: "Darwin")
    with pytest.raises(box.BoxError, match="another machine"):
        box.daemon_kind("docker", {"DOCKER_HOST": host})


@pytest.mark.parametrize("host", ["tcp://localhost:2375", "tcp://127.0.0.1:2375",
                                  "tcp://[::1]:2375", "unix:///var/run/docker.sock",
                                  "npipe:////./pipe/docker_engine"])
def test_this_machines_daemon_over_tcp_or_a_socket_is_local(host):
    """WSL1 and Docker Desktop's "expose on tcp://localhost:2375"."""
    assert box._is_remote(host) is False


def test_install_over_a_checkouts_box_reports_it_instead_of_replacing(tmp_path, monkeypatch):
    monkeypatch.setattr(box, "docker_bin", lambda: "docker")
    monkeypatch.setattr(box, "foreign_project", lambda exe, env=None: "aiforgecrew")
    monkeypatch.setattr(box, "running_image", lambda exe, env=None: "aiforge-sandbox:local")
    app = type("A", (), {"env": {}, "cfg": _cfg(tmp_path, "aiforge-sandbox:new")})()
    assert install._box_action_for_update(app) == "up"


# ── PATH, completion, the binary itself ───────────────────────────────────

def test_path_lines_are_added_once_and_removed_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "is_windows", lambda: False)
    rc = tmp_path / ".bashrc"
    rc.write_text("alias ll='ls -l'\n")
    env = {"HOME": str(tmp_path), "SHELL": "/bin/bash", "PATH": "/usr/bin"}
    folder = tmp_path / ".local" / "bin"
    assert install.add_to_path(folder, env) == [str(rc), str(tmp_path / ".profile")]
    assert install.add_to_path(folder, env) == []                 # idempotent
    assert f'export PATH="{folder}:$PATH"' in rc.read_text()
    install.remove_from_path(env)
    assert rc.read_text() == "alias ll='ls -l'\n"


def test_zsh_and_fish_get_their_own_startup_file(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "is_windows", lambda: False)
    folder = tmp_path / "bin"
    install.add_to_path(folder, {"HOME": str(tmp_path), "SHELL": "/bin/zsh"})
    assert f'export PATH="{folder}:$PATH"' in (tmp_path / ".zshrc").read_text()
    install.add_to_path(folder, {"HOME": str(tmp_path), "SHELL": "/usr/bin/fish"})
    fish = (tmp_path / ".config/fish/config.fish").read_text()
    assert f"set -gx PATH '{folder}' $PATH" in fish and "fish_add_path" not in fish
    # uninstall finds every file it wrote, whatever $SHELL is by then
    assert len(install.remove_from_path({"HOME": str(tmp_path), "SHELL": "/bin/bash"})) == 2
    assert "PATH" not in (tmp_path / ".config/fish/config.fish").read_text()


def test_bash_login_shells_read_bash_profile_when_there_is_one(tmp_path, monkeypatch):
    """macOS Terminal starts login shells, and bash then never reads .profile."""
    monkeypatch.setattr(install, "is_windows", lambda: False)
    (tmp_path / ".bash_profile").write_text("# mine\n")
    env = {"HOME": str(tmp_path), "SHELL": "/bin/bash", "PATH": "/usr/bin"}
    assert install.add_to_path(tmp_path / "bin", env) == [
        str(tmp_path / ".bashrc"), str(tmp_path / ".bash_profile")]
    assert not (tmp_path / ".profile").exists()


def test_a_folder_with_shell_characters_is_quoted(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "is_windows", lambda: False)
    install.add_to_path(Path("/home/a b/$x`y`"), {"HOME": str(tmp_path), "SHELL": "/bin/zsh"})
    assert 'export PATH="/home/a b/\\$x\\`y\\`:$PATH"' in (tmp_path / ".zshrc").read_text()


class _FakeWinreg:
    """Just enough of winreg for the user-PATH round trip."""
    HKEY_CURRENT_USER, KEY_READ, KEY_SET_VALUE, REG_SZ, REG_EXPAND_SZ = 1, 2, 3, 1, 2

    def __init__(self, value, kind=2):
        self.value, self.kind, self.writes = value, kind, []

    def OpenKey(self, *a):
        import contextlib
        return contextlib.nullcontext(self)

    def QueryValueEx(self, _k, _name):
        if self.value is None:
            raise FileNotFoundError
        return self.value, self.kind

    def SetValueEx(self, _k, _name, _r, kind, value):
        self.writes.append((value, kind))
        self.value, self.kind = value, kind


def test_the_windows_user_path_keeps_its_type_and_unexpanded_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "_broadcast_env_change", lambda: None)
    reg = _FakeWinreg(r"%USERPROFILE%\bin;C:\Tools", kind=_FakeWinreg.REG_EXPAND_SZ)
    folder = tmp_path / "Programs" / "AIForge"
    assert install._add_to_windows_path(folder, install._WinUserPath(reg)) == ["user PATH"]
    assert reg.writes == [(rf"%USERPROFILE%\bin;C:\Tools;{folder}", reg.REG_EXPAND_SZ)]
    assert install._add_to_windows_path(folder, install._WinUserPath(reg)) == []
    assert install._remove_from_windows_path(folder, install._WinUserPath(reg)) == ["user PATH"]
    assert reg.value == r"%USERPROFILE%\bin;C:\Tools"


def test_no_user_path_yet_is_created_not_crashed(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "_broadcast_env_change", lambda: None)
    reg = _FakeWinreg(None)
    install._add_to_windows_path(tmp_path, install._WinUserPath(reg))
    assert reg.writes == [(str(tmp_path), reg.REG_EXPAND_SZ)]


def test_the_binary_is_copied_atomically_and_only_when_it_is_elsewhere(tmp_path):
    src = tmp_path / "Downloads" / "aiforge"
    src.parent.mkdir()
    src.write_bytes(b"\x7fELF binary")
    dest = tmp_path / ".local" / "bin" / "aiforge"
    assert install.copy_self(src, dest) is True
    assert dest.read_bytes() == b"\x7fELF binary" and os.access(dest, os.X_OK)
    assert install.copy_self(dest, dest) is False                 # running the installed one


def test_a_binary_in_use_is_a_clear_error_not_a_traceback(tmp_path, monkeypatch):
    from aiforge_cli.app import Exit
    src = tmp_path / "aiforge"
    src.write_bytes(b"new")
    monkeypatch.setattr(install, "running_binary", lambda: src)

    def locked(_s, _d):
        (tmp_path / "bin").mkdir(exist_ok=True)
        (tmp_path / "bin" / "aiforge.new").write_bytes(b"half")
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(install, "copy_self", locked)
    monkeypatch.setattr(install, "is_windows", lambda: False)
    app = type("A", (), {"env": {"AIFORGE_BIN_DIR": str(tmp_path / "bin")}})()
    with pytest.raises(Exit, match="in use"):
        install.run_install(app)
    assert not (tmp_path / "bin" / "aiforge.new").exists()


def test_install_and_uninstall_followed_by_words_are_a_chat(tmp_path, monkeypatch):
    """`aiforge uninstall the unused npm deps` asks the agent; it must not
    delete aiforge."""
    from aiforge_cli import cli
    seen = []
    monkeypatch.setattr(cli, "_dispatch",
                        lambda app, command, rest, opts: seen.append((command, rest)) or 0)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    cli.main(["uninstall", "the", "unused", "npm", "deps"])
    cli.main(["uninstall"])
    assert seen == [(None, ["uninstall", "the", "unused", "npm", "deps"]), ("uninstall", [])]


def test_reinstalling_a_newer_binary_restarts_the_older_box(tmp_path, monkeypatch):
    monkeypatch.setattr(box, "docker_bin", lambda: "docker")
    monkeypatch.setattr(box, "foreign_project", lambda exe, env=None: "")
    app = type("A", (), {"env": {}, "cfg": _cfg(tmp_path, "aiforge-sandbox:new")})()
    monkeypatch.setattr(box, "running_image", lambda exe, env=None: "aiforge-sandbox:old")
    assert install._box_action_for_update(app) == "restart"
    monkeypatch.setattr(box, "running_image", lambda exe, env=None: "aiforge-sandbox:new")
    assert install._box_action_for_update(app) == "up"
    monkeypatch.setattr(box, "running_image", lambda exe, env=None: "")
    assert install._box_action_for_update(app) == "up"


def test_uninstall_from_source_leaves_pips_script_alone(monkeypatch):
    from aiforge_cli.app import Exit
    monkeypatch.setattr(install, "running_binary", lambda: None)
    monkeypatch.setattr(box, "stop", lambda *a, **k: pytest.fail("stopped the box"))
    with pytest.raises(Exit, match="pip uninstall aiforge-cli"):
        install.run_uninstall(object())


def test_install_from_source_points_at_pip(monkeypatch):
    from aiforge_cli.app import Exit
    monkeypatch.setattr(install, "running_binary", lambda: None)
    with pytest.raises(Exit, match="pip install -e packages/aiforge_cli"):
        install.run_install(object())
