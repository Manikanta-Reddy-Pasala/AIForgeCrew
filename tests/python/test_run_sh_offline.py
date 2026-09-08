"""run.sh installs nothing by downloading a source and executing it.

What it used to do, and what these tests pin as gone:

  ``curl https://astral.sh/uv/install.sh | sh``   a remote script into a shell
  a Node tarball off nodejs.org unpacked onto PATH
  uv's managed-CPython download
  ``playwright install chromium``                 ~150MB off a browser CDN

uv and Node are the ``toolchain`` extra now (the ``uv`` wheel and
``nodejs-wheel-binaries``), so they arrive the way every other dependency
does. What is left is only ever a package manager fetching a pinned artifact.

The absence of a download is proved with a fake ``curl``/``wget`` on PATH that
records being called: asserting on a message alone would pass just as happily
on a box where curl does not exist. The source text is checked too, because a
call site that is merely unreachable today is a call site that comes back.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RUN_SH = REPO / "run.sh"
SRC = RUN_SH.read_text()


def _code_lines() -> list[str]:
    """run.sh with comment-only lines dropped — history in a comment is fine,
    a live call site is not."""
    return [ln for ln in SRC.splitlines() if not ln.lstrip().startswith("#")]


# ── the four fetches are gone from the code, not just unreachable ───────────

@pytest.mark.parametrize("needle", [
    "astral.sh",            # the piped installer
    "nodejs.org/dist",      # the Node tarball
    "playwright install",   # the browser build
])
def test_no_call_site_fetches_a_toolchain(needle):
    hits = [ln for ln in _code_lines() if needle in ln]
    assert not hits, f"{needle} is still fetched: {hits}"


def test_the_wget_download_helper_is_gone():
    """It existed only to fetch those installers."""
    assert "_wget_https" not in SRC


def test_uv_never_installs_a_second_interpreter():
    assert 'UV_PYTHON_DOWNLOADS="${UV_PYTHON_DOWNLOADS:-never}"' in SRC


def test_the_toolchain_is_declared_as_a_python_extra():
    pyproject = (REPO / "pyproject.toml").read_text()
    extra = re.search(r"^toolchain = \[(.+?)\]", pyproject, re.M | re.S)
    assert extra, "no [project.optional-dependencies] toolchain extra"
    assert "uv" in extra.group(1)
    assert "nodejs-wheel-binaries" in extra.group(1)


def test_run_sh_installs_that_extra_rather_than_a_tarball():
    assert "'.[toolchain]'" in SRC


# ── and nothing reaches for them at runtime either ──────────────────────────

@pytest.fixture(scope="session")
def toolless_path(tmp_path_factory):
    """A PATH that has everything EXCEPT uv, node and npm.

    Dropping whole directories does not work: on most boxes /usr/bin holds
    node AND bash, so filtering by "contains node" removes the shell and every
    test dies with FileNotFoundError: 'bash'. So mirror each PATH entry as a
    farm of symlinks and simply leave the three tools out. Built once per
    session — it is a few thousand symlinks.
    """
    shim = tmp_path_factory.mktemp("nopath")
    hidden = {"uv", "node", "npm", "npx", "corepack"}
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d)
        if not p.is_dir():
            continue
        try:
            entries = list(p.iterdir())
        except OSError:
            continue
        for exe in entries:
            if exe.name in hidden or (shim / exe.name).exists():
                continue
            try:
                (shim / exe.name).symlink_to(exe)
            except OSError:
                pass
    return shim


@pytest.fixture
def box(tmp_path, toolless_path):
    """A machine with curl and wget but NO uv and NO node, and a clean HOME.

    The fakes exit non-zero: their job is to record an attempt, not to fake a
    successful install.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "fetched.txt"
    for tool in ("curl", "wget"):
        f = bin_dir / tool
        f.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{marker}"\nexit 1\n')
        f.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.pathsep.join([str(bin_dir), str(toolless_path)]),
        "HOME": str(home),
        "AIFORGE_CONFIG_DIR": str(home / ".aiforge"),
        "AIFORGE_INSTALL_TMUX": "0",
        "AIFORGE_OFFLINE": "1",
    }
    return {"env": env, "marker": marker, "tmp": tmp_path}


def _run(box, args, timeout=120):
    env = dict(os.environ)
    env.update(box["env"])
    return subprocess.run(["bash", str(RUN_SH), *args], cwd=str(REPO),
                          capture_output=True, text=True, env=env,
                          timeout=timeout)


def _fetched(box) -> str:
    return box["marker"].read_text() if box["marker"].exists() else ""


def test_a_missing_uv_never_reaches_astral_sh(box):
    r = _run(box, [])
    assert "astral.sh" not in _fetched(box), _fetched(box)
    assert "nodejs.org" not in _fetched(box), _fetched(box)
    # and it said what to do about it rather than dying mutely
    assert "uv" in (r.stdout + r.stderr).lower()


def test_offline_is_announced_and_refuses_the_wheel_too(box):
    r = _run(box, ["--offline"])
    assert "offline: no network at all" in r.stdout
    assert r.returncode != 0
    assert "package manager" in r.stderr


def test_offline_tells_the_tools_as_well_as_the_call_sites(box):
    """A dependency that shells out on its own has to be refused too."""
    for var in ("UV_OFFLINE", "PIP_NO_INDEX", "npm_config_offline",
                "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "HF_HUB_OFFLINE"):
        assert f'export {var}="${{{var}:-' in SRC, var
