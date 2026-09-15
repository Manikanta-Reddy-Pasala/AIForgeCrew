"""The mount list and its approvals — both host-side files.

``~/.aiforge/mounts.list`` is shared with the sandbox (Settings and the agent's
own mount_folder tool append to it), so a line in it is a REQUEST, not a
permission. ``~/.config/aiforge/approved-mounts`` is the host's answer, kept
where the box cannot reach it. Only the intersection is ever mounted — the same
rule run.sh enforces, so the two entry points cannot disagree.
"""

from __future__ import annotations

from pathlib import Path

from . import paths


def _lines(path: Path) -> list[str]:
    try:
        raw = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in raw:
        line = line.strip()
        if line and not line.startswith("#") and line not in out:
            out.append(line)
    return out


def requested(mounts_file: Path) -> list[str]:
    """Folders anyone has asked to mount."""
    return _lines(mounts_file)


def approved(approvals_file: Path) -> list[str]:
    """Folders this host has said yes to."""
    return _lines(approvals_file)


def effective(mounts_file: Path, approvals_file: Path, *,
              home: str | None = None, platform: str | None = None) -> list[str]:
    """What will actually be mounted on the next box start: requested AND
    approved AND still a sane folder to hand over."""
    ok = set(approved(approvals_file))
    return [m for m in requested(mounts_file)
            if m in ok and paths.mount_refusal(m, home=home, platform=platform) is None]


def pending(mounts_file: Path, approvals_file: Path) -> list[str]:
    """Requested but never approved — shown, never mounted."""
    ok = set(approved(approvals_file))
    return [m for m in requested(mounts_file) if m not in ok]


def add(mounts_file: Path, approvals_file: Path, path: str, *, approve: bool) -> None:
    """Record a request, and the host's approval when the user gave one.

    ``approve`` is True only for a path the USER named — the folder they ran in,
    or the argument to `aiforge mount add`. It is never inferred for a line the
    box wrote into mounts.list.
    """
    _append(mounts_file, path, mode=0o644)
    if approve:
        _append(approvals_file, path, mode=0o600)


def remove(mounts_file: Path, approvals_file: Path, path: str) -> None:
    for f in (mounts_file, approvals_file):
        keep = [line for line in _lines(f) if line != path]
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("".join(f"{line}\n" for line in keep))
        except OSError:
            pass


def _append(file: Path, value: str, *, mode: int) -> None:
    if value in _lines(file):
        return
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a") as fh:
        fh.write(value + "\n")
    try:
        file.chmod(mode)
    except OSError:
        pass
