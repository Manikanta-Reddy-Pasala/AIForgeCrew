"""The packaged app (.deb/.dmg/.msi/portable) installs what uv.lock pins.

Found by installing a .dmg and diffing it against the lock: the first run
resolved every dependency fresh from the index, so none of pyproject's
override-dependencies applied (they are a uv PROJECT setting and never reach
wheel metadata) and the app got starlette 0.52.1 — below the >=1.1.0 floor
that exists because <1.0 carries two HIGH CVEs. build_payload.sh now exports
the lock as lock-pins.txt and every first run installs it as --override — not
as constraints: google-adk 2.1.0 caps starlette <1.0 itself, so the lock is
unsatisfiable as -c and only reproducible the way uv.lock got it, by override.

Found by verify-deb.sh: the bootstrap ran uv in the directory it was launched
from, so a user starting the app inside a project inherited that project's
[tool.uv] index (this repo's names an estate-only Artifactory) and the install
died on DNS.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INST = REPO / "installer"


def test_the_payload_exports_the_lock_pins():
    src = (INST / "build_payload.sh").read_text()
    assert "uv export --frozen" in src and "--no-emit-local" in src
    assert 'lock-pins.txt' in src


def test_the_shipped_uv_is_the_locked_wheel_from_the_index():
    src = (INST / "build_payload.sh").read_text()
    assert 'pip download' in src and '"uv==$ver"' in src
    assert "curl" not in _code(INST / "build_payload.sh")


def _code(path: Path) -> str:
    """Non-comment lines — history in a comment is fine, a fetch is not."""
    return "\n".join(ln for ln in path.read_text().splitlines()
                     if not ln.lstrip().startswith("#"))


# Nothing is downloaded from GitHub — release assets, ghcr.io images, raw
# files, or uv's managed CPython (python-build-standalone lives on GitHub).
# Everything comes from a package index: PyPI, npm, or the estate's mirror.
_BUILD_AND_INSTALL = [
    REPO / "run.sh", REPO / "Dockerfile", REPO / "Makefile",
    *sorted(INST.rglob("*.sh")), INST / "windows" / "first-run.ps1",
    REPO / "scripts" / "install-graphify.sh", REPO / "scripts" / "install-embed-sidecar.sh",
]


def test_nothing_is_fetched_from_github():
    for f in _BUILD_AND_INSTALL:
        code = _code(f)
        for needle in ("github.com", "ghcr.io", "githubusercontent", "python install"):
            assert needle not in code, f"{f.relative_to(REPO)} still fetches: {needle}"


def test_no_interpreter_is_ever_downloaded():
    assert "export UV_PYTHON_DOWNLOADS=never" in (INST / "common" / "first-run.sh").read_text()
    assert "UV_PYTHON_DOWNLOADS = 'never'" in (INST / "windows" / "first-run.ps1").read_text()
    for f in ("scripts/install-graphify.sh", "scripts/install-embed-sidecar.sh", "Makefile"):
        assert "UV_PYTHON_DOWNLOADS" in (REPO / f).read_text(), f


def test_nothing_from_an_index_is_built_from_source():
    """Wheels only, everywhere: a source archive from an index is never built."""
    assert "--no-build" in (INST / "common" / "first-run.sh").read_text()
    assert "--no-build" in (INST / "windows" / "first-run.ps1").read_text()
    docker = (REPO / "Dockerfile").read_text()
    assert "uv pip install --system --no-config --no-build" in docker
    assert "-r /tmp/image-pins.txt --override /tmp/image-pins.txt" in docker
    assert "build-essential" not in _code(REPO / "Dockerfile")
    for f in ("build_payload.sh", "portable/build-portable.sh"):
        assert "--only-binary=:all:" in (INST / f).read_text(), f


def test_the_deb_declares_the_python_it_builds_on():
    assert "Depends: python3.12," in (INST / "linux" / "build-deb.sh").read_text()


def test_every_package_carries_the_lock_pins():
    for f in ("linux/build-deb.sh", "macos/build-dmg.sh",
              "portable/build-portable.sh", "windows/build-msi.sh"):
        assert "lock-pins.txt" in (INST / f).read_text(), f
    assert "lock-pins.txt" in (INST / "windows" / "first-run.ps1").read_text()


def _fake_app(tmp_path, python_found=True):
    app = tmp_path / "app"
    data = tmp_path / "data"
    launch = tmp_path / "some-project"
    for d in (app / "uv", launch):
        d.mkdir(parents=True)
    (launch / "pyproject.toml").write_text(
        '[[tool.uv.index]]\nname="x"\nurl="https://unreachable.invalid/simple/"\ndefault=true\n')
    (app / "aiforgecrew-9.9.9-py3-none-any.whl").write_text("")
    (app / "lock-pins.txt").write_text("starlette==1.6.0\n")
    log = tmp_path / "uv.log"
    # A fake uv: records cwd + args; `venv` makes a python, `pip install`
    # makes the console script the bootstrap then execs.
    uv = app / "uv" / "uv"
    uv.write_text(f"""#!/bin/sh
echo "cwd=$PWD args=$*" >> "{log}"
[ "$1 $2" = "python find" ] && exit {0 if python_found else 1}
case "$1" in
  venv) eval "v=\\${{$#}}"; mkdir -p "$v/bin"; printf '#!/bin/sh\\n' > "$v/bin/python"; chmod +x "$v/bin/python" ;;
  pip)  v="$(dirname "$(dirname "$4")")"; printf '#!/bin/sh\\necho APP-STARTED\\n' > "$v/bin/aiforge"; chmod +x "$v/bin/aiforge" ;;
esac
""")
    uv.chmod(0o755)
    return app, data, launch, log


def _first_run(app, data, launch):
    return subprocess.run(
        ["bash", str(INST / "common" / "first-run.sh")], cwd=str(launch),
        env={**os.environ, "AIFORGE_APP_HOME": str(app), "AIFORGE_DATA_HOME": str(data)},
        capture_output=True, text=True, timeout=60)


def test_first_run_overrides_with_the_lock_pins_from_its_data_dir(tmp_path):
    app, data, launch, log = _fake_app(tmp_path)
    r = _first_run(app, data, launch)
    assert "APP-STARTED" in r.stdout, r.stdout + r.stderr
    calls = log.read_text().splitlines()
    pip = [c for c in calls if " args=pip install" in c]
    assert pip, calls
    assert f"--override {app}/lock-pins.txt" in pip[0], pip[0]
    assert all(c.startswith(f"cwd={data} ") for c in calls), calls


def test_first_run_without_python_312_stops_and_downloads_nothing(tmp_path):
    app, data, launch, log = _fake_app(tmp_path, python_found=False)
    r = _first_run(app, data, launch)
    assert r.returncode == 1
    assert "needs Python 3.12" in r.stderr
    calls = log.read_text().splitlines()
    assert not [c for c in calls if " args=venv" in c or " args=pip" in c], calls
