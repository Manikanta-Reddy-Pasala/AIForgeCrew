"""The sandbox source the binary carries.

The `aiforge` binary is the ONE installer: it holds the few MB of AIForge
source the sandbox image is built from (what ./run.sh builds from a checkout),
so a machine with nothing but docker gets the sandbox, the web UI it serves,
and this CLI from one file. `installer/cli/build-binary.sh` packs it; here it is
found, unpacked once per content hash, and named as an image tag.

Run from source (not frozen) there is no payload — a checkout is the source,
and the run.sh strategy is used instead.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import sys
import tarfile
from pathlib import Path

NAME = "sandbox-src.tar.gz"
IMAGE_REPO = "aiforge-sandbox"


def tarball() -> Path | None:
    """The packed source inside this binary, or None when not frozen with one."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    p = Path(base) / "aiforge_cli" / "_payload" / NAME
    return p if p.is_file() else None


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def image_tag(path: Path | None = None) -> str | None:
    """`aiforge-sandbox:<hash of the carried source>` — a new binary with new
    source builds a new image; the same one reuses it."""
    path = path if path is not None else tarball()
    return f"{IMAGE_REPO}:{digest(path)}" if path else None


def extract(dest_root: Path, path: Path | None = None) -> Path:
    """Unpack the source to ``dest_root/src-<hash>`` (once) and return it.

    Unpacked beside the CLI's own compose file, NOT into ~/.aiforge: that folder
    is mounted into the box. Written to a temp dir and renamed, so a half
    unpack never looks finished."""
    path = path if path is not None else tarball()
    if path is None:
        raise FileNotFoundError("this aiforge has no sandbox source inside it")
    dest = dest_root / f"src-{digest(path)}"
    if (dest / "Dockerfile").is_file():
        return dest
    dest_root.mkdir(parents=True, exist_ok=True)
    tmp = dest_root / f".src-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    with tarfile.open(path, "r:gz") as tar:
        _safe_extract(tar, tmp)
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(dest)
    tmp.rename(dest)
    # Older unpacks are a previous version's: keep the folder from growing.
    for old in dest_root.glob("src-*"):
        if old != dest:
            shutil.rmtree(old, ignore_errors=True)
    return dest


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Refuse members that would land outside ``dest`` (absolute paths, `..`,
    links out) — the archive is ours, but extraction should not have to trust
    that."""
    root = dest.resolve()
    for m in tar.getmembers():
        target = (dest / m.name).resolve()
        if root != target and root not in target.parents:
            raise ValueError(f"refusing to unpack {m.name!r}: outside the target")
        if m.issym() or m.islnk():
            link = (target.parent / m.linkname).resolve()
            if root != link and root not in link.parents:
                raise ValueError(f"refusing link {m.name!r} -> {m.linkname!r}")
    if hasattr(tarfile, "data_filter"):           # 3.11.4+: the stdlib's own guard too
        tar.extractall(dest, filter="data")
    else:  # pragma: no cover
        tar.extractall(dest)  # noqa: S202 — every member checked above
