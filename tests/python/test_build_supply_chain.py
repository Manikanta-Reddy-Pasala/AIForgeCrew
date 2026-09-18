"""What the build and the sandbox install fetch — and from where.

The native packages (.deb/.dmg/.msi/portable) are retired: AIForge installs
from the `aiforge` binary, which builds the sandbox from the source it carries
(installer/cli/build-binary.sh → Dockerfile → run.sh inside the box). These
checks keep that path honest: nothing from GitHub, no interpreter downloads,
no source archives built, no public-registry fallback.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INST = REPO / "installer"


def _code(path: Path) -> str:
    """Non-comment lines — history in a comment is fine, a fetch is not."""
    return "\n".join(ln for ln in path.read_text().splitlines()
                     if not ln.lstrip().startswith("#"))


# Nothing is downloaded from GitHub — release assets, ghcr.io images, raw
# files, or uv's managed CPython (python-build-standalone lives on GitHub).
_BUILD_AND_INSTALL = [
    REPO / "run.sh", REPO / "Dockerfile", REPO / "Makefile",
    *sorted(INST.rglob("*.sh")),
    REPO / "scripts" / "install-graphify.sh", REPO / "scripts" / "install-embed-sidecar.sh",
]


def test_nothing_is_fetched_from_github():
    for f in _BUILD_AND_INSTALL:
        code = _code(f)
        for needle in ("github.com", "ghcr.io", "githubusercontent", "python install"):
            assert needle not in code, f"{f.relative_to(REPO)} still fetches: {needle}"


def test_no_interpreter_is_ever_downloaded():
    for f in ("scripts/install-graphify.sh", "scripts/install-embed-sidecar.sh", "Makefile"):
        assert "UV_PYTHON_DOWNLOADS" in (REPO / f).read_text(), f


def test_nothing_from_an_index_is_built_from_source():
    # the image carries no compiler to build anything with
    assert "build-essential" not in _code(REPO / "Dockerfile")
    # the binary's build tools come as wheels only
    assert "--only-binary=:all:" in (INST / "cli" / "build-binary.sh").read_text()


def test_there_is_no_public_registry_fallback():
    """The estate reaches only the internal Artifactory: an unresolvable index
    stops the build instead of switching to pypi.org."""
    for f in _BUILD_AND_INSTALL:
        code = _code(f)
        for host in ("pypi.org", "pythonhosted.org", "registry.npmjs.org"):
            assert host not in code, f"{f.relative_to(REPO)} names {host}"
    # run.sh installs npm packages inside the box through AIFORGE_NPM_REGISTRY;
    # the image itself runs no npm at build.
    assert "npm " not in _code(REPO / "Dockerfile")


def test_the_binary_carries_what_the_dockerfile_copies():
    """`aiforge install` builds the sandbox from the source packed into the
    binary: every path the Dockerfile COPYs must be in that pack."""
    build = (INST / "cli" / "build-binary.sh").read_text()
    packed = build.split("SRC_PATHS=(", 1)[1].split(")", 1)[0].split()
    copied = set()
    for ln in (REPO / "Dockerfile").read_text().splitlines():
        if ln.startswith("COPY ") and "--from" not in ln:
            srcs = ln.split()[1:-1]
            copied.update(s.rstrip("/") for s in srcs)
    copied.discard("docker/entrypoint.sh")          # inside docker/, packed as a folder
    assert copied <= set(packed), sorted(copied - set(packed))


def test_the_binary_build_always_names_its_index():
    """pip ignores [[tool.uv.index]]: with no --index-url it would quietly use
    public PyPI. The build passes one on every run — the env's, else
    pyproject's default — and stops when there is none."""
    build = _code(INST / "cli" / "build-binary.sh")
    assert 'PIP_ARGS=(--disable-pip-version-check --index-url "$INDEX")' in build
    assert "tool\\.uv\\.index" in build
    assert 'no package index' in build
    assert "[[ -n \"${UV_DEFAULT_INDEX" not in build   # the old conditional


def test_the_pack_is_reproducible_lf_and_secret_free(tmp_path):
    """The image tag is the pack's hash: same source → same bytes. Scripts go
    in with LF whatever the checkout did, and untracked secrets stay out."""
    import subprocess
    import sys
    import tarfile

    src = tmp_path / "src"
    src.mkdir()
    (src / "run.sh").write_bytes(b"#!/usr/bin/env bash\r\necho hi\r\n")
    (src / "run.sh").chmod(0o755)
    (src / "keep.py").write_text("x = 1\n")
    (src / ".env").write_text("TOKEN=s3cret\n")
    (src / "id.pem").write_text("-----BEGIN-----\n")
    git = ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t",
           "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "run.sh"], check=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True)
    pack = REPO / "installer" / "cli" / "pack_source.py"
    outs = []
    for i in range(2):
        out = tmp_path / f"p{i}.tar.gz"
        subprocess.run([sys.executable, str(pack), str(src), str(out), "."], check=True,
                       capture_output=True)
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]
    assert outs[0][4:8] == b"\0\0\0\0"          # no gzip timestamp: same bytes any day
    with tarfile.open(tmp_path / "p0.tar.gz") as tar:
        names = sorted(tar.getnames())
        assert names == ["keep.py", "run.sh"]                 # untracked-but-safe too
        member = tar.getmember("run.sh")
        assert member.mode & 0o111
        assert tar.extractfile(member).read() == b"#!/usr/bin/env bash\necho hi\n"
