"""Pack the sandbox source the `aiforge` binary carries.

    python installer/cli/pack_source.py ROOT OUT.tar.gz PATH...

Exactly the files git knows about under PATH... (tracked, plus new files not
yet added that .gitignore does not exclude), so a local build packs what a
clean checkout would build from — and never a stray `.env`, key or database
that happens to sit in the tree. Not a git worktree: refuse, rather than guess.

Reproducible: sorted members, fixed mtime/owner, no gzip timestamp. The image
tag is the hash of this file, so the same source always names the same image.

Text files that run inside the Linux box (shell scripts, the Dockerfile, env
files) are packed with LF endings: a Windows checkout with core.autocrlf would
otherwise ship `#!/usr/bin/env bash\\r`, and the box dies at the shebang.
"""

from __future__ import annotations

import gzip
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

EXCLUDE_PREFIXES = ("packages/aiforge_vscode/",)   # the editor extension: not in the box
LF_SUFFIXES = (".sh", ".env", ".py", ".toml", ".yml", ".yaml", ".lock")
LF_NAMES = ("Dockerfile", "Makefile", ".dockerignore")
# The secret patterns from .dockerignore. docker build drops them anyway, but
# the binary is a download: a key sitting untracked in the tree must not ride
# along inside it. Tracked samples (.env.example) are fine.
SECRET_SUFFIXES = (".pem", ".key", ".db")


def looks_secret(name: str) -> bool:
    parts = name.split("/")
    base = parts[-1]
    return (base == ".env" or (base.startswith(".env.") and base != ".env.example")
            or base.endswith(SECRET_SUFFIXES) or "security" in parts[:-1]
            or ".aiforge" in parts)


def git_files(root: Path, paths: list[str]) -> list[str]:
    r = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard", "--", *paths],
        capture_output=True)
    if r.returncode != 0:
        sys.exit(f"pack_source: `git ls-files` failed in {root} — build from a git "
                 f"checkout (a safe.directory refusal counts):\n"
                 f"{r.stderr.decode(errors='replace').strip()}")
    names = {n for n in r.stdout.decode().split("\0") if n}
    return sorted(n for n in names
                  if not n.startswith(EXCLUDE_PREFIXES) and not looks_secret(n)
                  and (root / n).is_file())


def git_exec_bits(root: Path) -> set[str]:
    """Tracked files git records as executable (mode 100755). Read from git,
    not the filesystem: on Windows stat() says nothing about run.sh."""
    r = subprocess.run(["git", "-C", str(root), "ls-files", "-s", "-z"], capture_output=True)
    out = set()
    for rec in r.stdout.decode().split("\0"):
        meta, _, name = rec.partition("\t")
        if meta.startswith("100755"):
            out.add(name)
    return out


def wants_lf(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return base in LF_NAMES or base.endswith(LF_SUFFIXES)


def pack(root: Path, out: Path, paths: list[str]) -> int:
    files = git_files(root, paths)
    execs = git_exec_bits(root)
    if not files:
        sys.exit("pack_source: nothing to pack")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in files:
            src = root / name
            data = src.read_bytes()
            if wants_lf(name) or data.startswith(b"#!"):   # any script, suffix or not
                data = data.replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            # Keep the executable bit (run.sh, entrypoint.sh); nothing else.
            executable = name in execs or (os.name != "nt" and src.stat().st_mode & 0o111)
            info.mode = 0o755 if executable else 0o644
            tar.addfile(info, io.BytesIO(data))
    with out.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0,
                                              filename="") as gz:
        gz.write(buf.getvalue())
    return len(files)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.exit(__doc__)
    root, out, paths = Path(argv[0]).resolve(), Path(argv[1]), argv[2:]
    n = pack(root, out, paths)
    print(f"packed {n} files -> {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
