"""`aiforge install` / `aiforge uninstall` — the one installer.

Download the binary for your OS, run `aiforge install`, and you have all three:
this CLI on your PATH, the sandbox (built from the source the binary carries),
and the web UI the sandbox serves. Nothing else is installed on the host —
docker is the only prerequisite.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from pathlib import Path

from . import box, completion, payload

MARK = "# added by `aiforge install`"


def is_windows() -> bool:
    return os.name == "nt"


def bin_dir(env: dict[str, str] | None = None) -> Path:
    """Per-user, no admin: ~/.local/bin, or %LOCALAPPDATA%\\Programs\\AIForge."""
    env = os.environ if env is None else env
    if is_windows():
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Programs" / "AIForge"
    return Path(env.get("AIFORGE_BIN_DIR") or (Path.home() / ".local" / "bin"))


def target(env: dict[str, str] | None = None) -> Path:
    return bin_dir(env) / ("aiforge.exe" if is_windows() else "aiforge")


def running_binary() -> Path | None:
    """This executable when it is the frozen binary; None when run from source."""
    return Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None


def copy_self(src: Path, dest: Path) -> bool:
    """Put the binary at ``dest`` (atomically: copy beside it, then replace).
    False when it is already there."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.resolve() == src.resolve():
        return False
    tmp = dest.with_name(dest.name + ".new")
    shutil.copy2(src, tmp)
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)
    return True


def on_path(folder: Path, env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    parts = [Path(p) for p in env.get("PATH", "").split(os.pathsep) if p]
    return any(_same(p, folder) for p in parts)


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def _home(env: dict[str, str]) -> Path:
    return Path(env.get("HOME") or Path.home())


def rc_files(env: dict[str, str] | None = None) -> list[Path]:
    """The shell start-up files that should put the folder on PATH.

    bash: .bashrc for interactive shells, plus the LOGIN file — .bash_profile
    when there is one (then bash never reads .profile; macOS Terminal starts
    login shells), else .profile."""
    env = os.environ if env is None else env
    home = _home(env)
    shell = Path(env.get("SHELL", "")).name
    if shell == "zsh":
        return [home / ".zshrc"]
    if shell == "fish":
        return [home / ".config" / "fish" / "config.fish"]
    login = home / ".bash_profile"
    return [home / ".bashrc", login if login.exists() else home / ".profile"]


def all_rc_files(env: dict[str, str] | None = None) -> list[Path]:
    """Every file add_to_path may have written, whatever $SHELL is now."""
    env = os.environ if env is None else env
    home = _home(env)
    return [home / n for n in (".bashrc", ".bash_profile", ".profile", ".zshrc")] + [
        home / ".config" / "fish" / "config.fish"]


def _path_line(rc: Path, folder: Path) -> str:
    if rc.name == "config.fish":
        # Not fish_add_path: that writes a universal variable, which outlives
        # this line (uninstall could not take it back).
        q = "'" + str(folder).replace("\\", "\\\\").replace("'", "\\'") + "'"
        return f"contains {q} $PATH; or set -gx PATH {q} $PATH"
    q = str(folder).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") \
        .replace("`", "\\`")
    return f'export PATH="{q}:$PATH"'


def add_to_path(folder: Path, env: dict[str, str] | None = None) -> list[str]:
    """Make ``folder`` part of PATH for new terminals. Returns what changed."""
    if is_windows():
        return _add_to_windows_path(folder)
    changed = []
    for rc in rc_files(env):
        text = rc.read_text() if rc.exists() else ""
        if MARK in text:
            continue
        rc.parent.mkdir(parents=True, exist_ok=True)
        with rc.open("a") as fh:
            fh.write(f"\n{MARK}\n{_path_line(rc, folder)}\n")
        changed.append(str(rc))
    return changed


def remove_from_path(env: dict[str, str] | None = None) -> list[str]:
    """Undo add_to_path (POSIX start-up files; the Windows user PATH)."""
    if is_windows():
        return _remove_from_windows_path(bin_dir(env))
    changed = []
    for rc in all_rc_files(env):
        if not rc.exists():
            continue
        lines = rc.read_text().splitlines(keepends=True)
        out, skip = [], False
        for ln in lines:
            if ln.strip() == MARK:
                skip = True
                if out and not out[-1].strip():      # the blank line add_to_path wrote
                    out.pop()
                continue
            if skip:
                skip = False
                continue
            out.append(ln)
        if len(out) != len(lines):
            rc.write_text("".join(out))
            changed.append(str(rc))
    return changed


# ── Windows: the user PATH lives in the registry ──────────────────────────
# Read and written with winreg, keeping the value's own type: REG_EXPAND_SZ
# entries such as %USERPROFILE%\bin stay unexpanded. (PowerShell's
# [Environment]::Get/SetEnvironmentVariable expands them for good, and piping
# its output through a code page mangled any non-ASCII folder name.)


class _WinUserPath:
    KEY = "Environment"

    def __init__(self, winreg=None):
        if winreg is None:
            import winreg as _w
            winreg = _w
        self.w = winreg

    def read(self) -> "tuple[str, int]":
        w = self.w
        with w.OpenKey(w.HKEY_CURRENT_USER, self.KEY, 0, w.KEY_READ) as k:
            try:
                val, kind = w.QueryValueEx(k, "Path")
            except FileNotFoundError:
                return "", w.REG_EXPAND_SZ
        return str(val or ""), kind

    def write(self, value: str, kind: int) -> None:
        w = self.w
        with w.OpenKey(w.HKEY_CURRENT_USER, self.KEY, 0, w.KEY_SET_VALUE) as k:
            w.SetValueEx(k, "Path", 0, kind, value)
        _broadcast_env_change()


def _broadcast_env_change() -> None:
    """Tell Explorer the environment changed, so NEW terminals see it."""
    with contextlib.suppress(Exception):
        import ctypes
        result = ctypes.c_size_t()          # a DWORD_PTR: pointer-sized
        ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))


def _win_same(entry: str, folder: Path) -> bool:
    return _same(Path(os.path.expandvars(entry)), folder)


def _add_to_windows_path(folder: Path, reg: "_WinUserPath | None" = None) -> list[str]:
    reg = reg or _WinUserPath()
    cur, kind = reg.read()
    if any(_win_same(p, folder) for p in cur.split(";") if p):
        return []
    reg.write(f"{cur.rstrip(';')};{folder}" if cur else str(folder), kind)
    return ["user PATH"]


def _remove_from_windows_path(folder: Path, reg: "_WinUserPath | None" = None) -> list[str]:
    reg = reg or _WinUserPath()
    cur, kind = reg.read()
    parts = [p for p in cur.split(";") if p]
    keep = [p for p in parts if not _win_same(p, folder)]
    if len(keep) == len(parts):
        return []
    reg.write(";".join(keep), kind)
    return ["user PATH"]


def completion_file(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return _home(env) / ".local" / "share" / "bash-completion" / "completions" / "aiforge"


def install_completion(env: dict[str, str] | None = None) -> str | None:
    """bash completion where bash-completion looks for it (no rc edit)."""
    env = os.environ if env is None else env
    if is_windows() or Path(env.get("SHELL", "")).name not in ("bash", ""):
        return None
    dest = completion_file(env)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(completion.script("bash"))
    return str(dest)


# ── the two commands ─────────────────────────────────────────────────────


def run_install(app) -> int:
    """Binary → PATH, completion, then the sandbox (built from the carried
    source; it serves the web UI). Idempotent: re-running updates the binary
    and brings the box up."""
    from .app import EXIT_ENV, EXIT_OK, EXIT_USAGE, Exit
    src = running_binary()
    if src is None:
        raise Exit(EXIT_USAGE, "`aiforge install` installs the downloaded aiforge binary. "
                   "From a checkout: pip install -e packages/aiforge_cli (then aiforge).")
    dest = target(app.env)
    try:
        copied = copy_self(src, dest)
    except PermissionError as exc:
        with contextlib.suppress(OSError):
            dest.with_name(dest.name + ".new").unlink()
        raise Exit(EXIT_ENV, f"cannot replace {dest}: it is in use ({exc.strerror}). "
                   f"Close every other aiforge window, then re-run this.") from exc
    app.ok("aiforge installed" if copied else "aiforge is installed", str(dest))
    if not on_path(dest.parent, app.env):
        changed = add_to_path(dest.parent, app.env)
        if changed:
            app.ok("added to PATH", ", ".join(changed) + " — open a new terminal")
    done = install_completion(app.env)
    if done:
        app.ok("tab completion", done)
    code = app.box_command([_box_action_for_update(app)])
    if code != EXIT_OK:
        return code
    app.say("", f"  next: cd into a project and run {app.pal('aiforge', 'ok')}",
            f"  web UI: {app.cfg.base_url}/ui/   ·   uninstall: aiforge uninstall")
    return EXIT_OK


def _box_action_for_update(app) -> str:
    """`up`, or `restart` when the running box is an OLDER binary's: a new
    binary carries new source, so its image tag differs — and re-running
    install is how you update (restart refuses while a run is in flight)."""
    exe = box.docker_bin()
    if exe is None or app.cfg.repo is not None or box.foreign_project(exe, app.env):
        # A checkout's run.sh box is not ours to replace: `up` reports it.
        return "up"
    current = box.running_image(exe, app.env)
    return "restart" if current and current != app.cfg.image else "up"


def run_uninstall(app) -> int:
    """Stop the sandbox; remove the binary, its PATH lines, its completion and
    the source it unpacked. Your data (~/.aiforge), the box's state volume and
    the image stay — and it says how to remove those too."""
    from .app import EXIT_OK, EXIT_USAGE, Exit
    if running_binary() is None:
        # From source, ~/.local/bin/aiforge is pip's own script, not ours.
        raise Exit(EXIT_USAGE, "`aiforge uninstall` removes an installed aiforge binary. "
                   "Installed with pip: pip uninstall aiforge-cli.")
    name = box.container_name(app.env)
    app._refuse_if_busy("stop the sandbox")     # like `box down`: --force overrides
    try:
        stopped = box.stop(app.cfg, app.env)
    except box.BoxError:
        stopped = None                    # docker not installed / not running
    if stopped is None:
        app.ok("sandbox not running", "docker is not up")
    elif stopped:
        app.ok("sandbox stopped")
    else:
        app.warn(f"the sandbox `{name}` is still running (started by ./run.sh? "
                 f"stop it with ./run.sh --stop, or docker stop {name})")
    dest = target(app.env)
    here = running_binary()
    if dest.exists():
        if is_windows() and here is not None and _same(here, dest):
            # A running .exe cannot delete itself: leave a note instead.
            app.warn(f"delete {dest} after this window closes")
        else:
            try:
                dest.unlink()
                app.ok("removed", str(dest))
            except PermissionError:
                app.warn(f"{dest} is in use by another aiforge window — delete it "
                         f"once that is closed")
    for what in remove_from_path(app.env):
        app.ok("PATH entry removed", what)
    comp = completion_file(app.env)
    if comp.exists():
        comp.unlink()
        app.ok("removed", str(comp))
    unpacked = box.compose_path(app.env).parent
    if unpacked.is_dir():
        shutil.rmtree(unpacked, ignore_errors=True)
        app.ok("removed", str(unpacked))
    app.say(f"  kept: your chats, settings and memory in {app.cfg.config_dir}",
            "  to remove the sandbox itself too:",
            f"    docker rm -f {name}",
            f"    docker volume rm {box.project_name(app.env)}_aiforge-state",
            f"    docker rmi $(docker images {payload.IMAGE_REPO} -q)")
    return EXIT_OK
