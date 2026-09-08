"""run.sh never fetches a SOURCE and executes it.

That was the whole objection, and it is now structural rather than a setting:

  ``curl https://astral.sh/uv/install.sh | sh``   a remote script into a shell
  a Node tarball off nodejs.org unpacked onto PATH
  uv's managed-CPython download
  ``playwright install chromium``                 a browser build off a CDN

All four are gone. uv and Node are the ``toolchain`` extra — ordinary wheels,
resolved from the same index, lockfile, private mirror and CA as every other
dependency. There is no switch here because there is nothing left for one to
guard: what still fetches is a package manager installing what the project
declares, which was never the thing that was dangerous.

The absence of a download is proved with a fake ``curl``/``wget`` on PATH that
records being called: asserting on a message alone would pass just as happily
on a box where curl does not exist. The source text is checked too, because a
call site that is merely unreachable today is one that comes back.
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


def _ca_bootstrap(tmp_path, ca_pem: str):
    """Run run.sh's own _ca_bootstrap with a corporate CA in place."""
    home = tmp_path / "home"
    (home / ".aiforge" / "security" / "ca").mkdir(parents=True)
    (home / ".aiforge" / "security" / "ca" / "custom-ca.pem").write_text(ca_pem)
    fn = re.search(r"^_system_ca_file\(\).*?^_ca_bootstrap$", SRC, re.S | re.M)
    assert fn, "run.sh no longer defines the CA bootstrap"
    script = f'set -euo pipefail\n{fn.group(0)}\necho "PUBLISHED=$SSL_CERT_FILE"\n'
    r = subprocess.run(["bash", "-c", script], cwd=str(tmp_path), text=True,
                       capture_output=True, timeout=30,
                       env={"HOME": str(home), "PATH": os.environ.get("PATH", ""),
                            "AIFORGE_CONFIG_DIR": str(home / ".aiforge")})
    assert r.returncode == 0, r.stderr
    published = re.search(r"PUBLISHED=(\S+)", r.stdout)
    assert published, r.stdout
    return Path(published.group(1))


@pytest.fixture
def a_ca(tmp_path):
    """A throwaway self-signed root, as an operator's corporate CA."""
    out = tmp_path / "corp.pem"
    r = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(tmp_path / "k.pem"), "-out", str(out), "-days", "2",
         "-subj", "/CN=Acme Corp Root"],
        capture_output=True, timeout=60)
    if r.returncode != 0:
        pytest.skip("openssl unavailable")
    return out.read_text()


def _count_certs(p: Path) -> int:
    return p.read_text().count("BEGIN CERTIFICATE")


def _code_lines() -> list[str]:
    """run.sh with comment-only lines dropped — history in a comment is fine,
    a live call site is not."""
    return [ln for ln in SRC.splitlines() if not ln.lstrip().startswith("#")]


# ── the four fetches are gone from the code, not merely unreachable ─────────

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


def test_the_browser_build_is_refused_permanently_not_by_a_setting():
    """It comes from a CDN, not a package index, so it is never fetched."""
    assert 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="${PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD:-1}"' in SRC


def test_the_toolchain_is_declared_as_a_python_extra():
    pyproject = (REPO / "pyproject.toml").read_text()
    extra = re.search(r"^toolchain = \[(.+?)\]", pyproject, re.M | re.S)
    assert extra, "no [project.optional-dependencies] toolchain extra"
    assert "uv" in extra.group(1)
    assert "nodejs-wheel-binaries" in extra.group(1)


def test_run_sh_installs_that_extra_rather_than_a_tarball():
    assert "'.[toolchain]'" in SRC


def test_there_is_no_network_switch_left_to_get_wrong():
    """A switch could only ever have stopped a box installing its own declared
    dependencies — which on a fresh clone means it cannot run."""
    for gone in ("AIFORGE_OFFLINE", "_offline", "_no_fetch", "--online", "--offline"):
        assert gone not in SRC, f"{gone} is back"


# ── and nothing reaches for a toolchain at runtime either ───────────────────

def test_a_missing_uv_never_reaches_astral_sh(box):
    r = _run(box, [])
    assert "astral.sh" not in _fetched(box), _fetched(box)
    assert "nodejs.org" not in _fetched(box), _fetched(box)
    assert "uv" in (r.stdout + r.stderr).lower()


def test_the_script_writes_no_configuration_at_all(box, tmp_path):
    """The env file is fixed and committed; a run must not edit it."""
    import shutil
    script = tmp_path / "run.sh"
    shutil.copy(RUN_SH, script)
    env = tmp_path / "aiforge.env"
    env.write_text("AIFORGE_LM_BASE_URL=http://127.0.0.1:1234/v1\n")
    before = env.read_text()
    subprocess.run(["bash", str(script)], cwd=str(tmp_path), text=True,
                   capture_output=True, timeout=90,
                   env={**os.environ, **box["env"]})
    assert env.read_text() == before
    assert not (tmp_path / ".env").exists()


# ── the CA bootstrap must ADD to the trust store, not replace it ────────────
# Reported live from WSL behind a corporate proxy: SSL_CERT_FILE REPLACES the
# trust store, so publishing a corporate-root-only file fixed the internal
# hosts and took every public root away with it — PyPI then failed with
# `invalid peer certificate: UnknownIssuer`.

def test_the_published_bundle_keeps_the_public_roots(tmp_path, a_ca):
    published = _ca_bootstrap(tmp_path, a_ca)
    system = next((Path(c) for c in (
        "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/ca-bundle.pem", "/etc/ssl/cert.pem") if Path(c).is_file()), None)
    if system is None:
        pytest.skip("no system CA bundle on this box")
    assert _count_certs(published) == _count_certs(system) + 1, (
        "the published bundle is not system roots + the operator's CA")


def test_the_published_bundle_still_carries_the_operators_ca(tmp_path, a_ca):
    published = _ca_bootstrap(tmp_path, a_ca)
    assert a_ca.strip() in published.read_text()


# ── a network failure must not delete a working venv ────────────────────────

def test_a_network_failure_does_not_delete_the_venv():
    """The rebuild branch is for a half-written venv on DrvFs, not for a proxy."""
    guard = re.search(r"if ! _out=.*?rm -rf \.venv", SRC, re.S)
    assert guard, "the deps-install failure branch changed shape"
    assert "certificate" in guard.group(0)
    assert "left ALONE" in guard.group(0)


def test_the_diagnosis_does_not_fire_on_a_bare_word(tmp_path):
    """`network` and `connect` appear in messages that are not link failures."""
    m = re.search(r"grep -qiE \"([^\"]*certificate[^\"]*)\"", SRC)
    assert m, "the failure classifier changed shape"
    pattern = m.group(1)
    assert "|network|" not in f"|{pattern}|"
    assert "|connect|" not in f"|{pattern}|"
