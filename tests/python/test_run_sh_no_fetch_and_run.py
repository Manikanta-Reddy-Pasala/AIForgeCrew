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
import shutil
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
        # run.sh now pip-installs the uv wheel on a box without uv. The test is
        # about what is NOT fetched, so give pip nowhere to fetch from.
        "PIP_NO_INDEX": "1",
    }
    return {"env": env, "marker": marker, "tmp": tmp_path}


def _run(box, args, timeout=120):
    """Run run.sh in an ISOLATED workspace — never in the repository itself.

    cwd used to be REPO. On a developer box with no .venv that happened to
    behave, but CI builds the .venv first, and the `toolchain` extra puts a uv
    INSIDE it. run.sh looks there (`.venv/bin/uv`), finds the project already
    installed, and goes on to launch the whole stack — which never returns, so
    the test died on its own 120s timeout instead of on anything it asserts.

    The box fixture is a machine WITHOUT uv. A checkout with a populated .venv
    is not that machine, so give run.sh an empty directory to be that machine
    in. It creates its own .venv there (stdlib only), asks pip for the uv wheel,
    pip has no index (PIP_NO_INDEX), and run.sh exits naming uv — curl and
    wget never run.
    """
    work = box["tmp"] / "work"
    if not work.exists():
        work.mkdir()
        shutil.copy(RUN_SH, work / "run.sh")
    env = dict(os.environ)
    env.update(box["env"])
    return subprocess.run(["bash", str(work / "run.sh"), *args], cwd=str(work),
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




# ── what run.sh DOES install: exactly what a lockfile pins ──────────────────
# A clean box used to stop four times — no uv, no project in .venv, no
# node_modules, no codegraph — each with a command for the operator to type.
# run.sh now installs those itself, but only from the three committed locks:
# uv.lock, web/package-lock.json and scripts/codegraph/package-lock.json.

def _executable_lines() -> list[str]:
    """Code lines with every quoted string blanked out: a command inside
    `echo "…"` is printed for the operator, not run, and must not be flagged.
    Lines that ARE a print (`echo`, `printf`, a hint-table arm) are dropped."""
    out = []
    for ln in _code_lines():
        stripped = ln.strip()
        if stripped.startswith(("echo ", "printf ")) or re.match(r"^\S+\)\s+echo ", stripped):
            continue
        out.append(re.sub(r"\"[^\"]*\"|'[^']*'", '""', ln))
    return out


def test_no_os_package_manager_or_global_install_runs():
    for cmd in ("apt-get install", "apt install", "brew install", "dnf install",
                "uv tool install", "pipx install", "npm install", "npm i ",
                "install-codegraph.sh", "uv pip install"):
        hits = [ln for ln in _executable_lines() if cmd in ln]
        assert not hits, f"run.sh runs an unpinned/OS install: {hits}"


def test_pip_only_ever_bootstraps_uv():
    """Python's pip runs once, to put the uv wheel in .venv; everything else is
    uv installing the lock's pins. A second pip would be an unpinned side channel."""
    lines = _executable_lines()
    hits = [i for i, ln in enumerate(lines) if "-m pip install" in ln]
    assert len(hits) == 1, [lines[i] for i in hits]
    # the one package it installs: uv, at the version uv.lock pins
    assert '"uv${_uvv:+==$_uvv}"' in SRC and '_uvv="$(_lock_version uv)"' in SRC


def test_python_deps_are_the_locks_versions_from_the_index_as_wheels():
    """Exported pins (names + versions only) installed from the configured
    index — never `uv sync`, which downloads the file URLs recorded in the lock
    (pypi.org). Every index package --no-build; then only the local packages."""
    code = "\n".join(_code_lines())
    assert "uv sync" not in code
    assert '"$UV" export --frozen' in code
    assert '--no-config --default-index "$_INDEX"' in code
    assert '--no-build -r "$_pins" --override "$_pins"' in code
    assert "--no-deps -e ./packages/aiforge_memory -e ." in code


def test_there_is_no_public_registry_fallback():
    """The estate reaches only the internal Artifactory."""
    code = "\n".join(_code_lines())
    for host in ("pypi.org", "pythonhosted.org", "registry.npmjs.org"):
        assert host not in code, host


def test_npm_installs_go_to_the_configured_registry():
    env = (REPO / "aiforge.env").read_text()
    assert "AIFORGE_NPM_REGISTRY=https://artifactory." in env
    assert 'npm_config_registry="$reg"' in SRC


def test_the_uv_bootstrap_is_a_wheel():
    assert "pip install -q --no-input --disable-pip-version-check --only-binary=:all:" in SRC


def test_every_npm_install_is_ci_from_a_lock_with_no_scripts():
    npm = [ln for ln in _executable_lines() if re.search(r"\bnpm (ci|i|install)\b", ln)]
    assert npm, "run.sh no longer installs web deps"
    for ln in npm:
        assert "npm ci --ignore-scripts" in ln, ln


def test_codegraph_is_pinned_exactly_by_a_committed_lock():
    import json
    d = REPO / "scripts" / "codegraph"
    pin = json.loads((d / "package.json").read_text())["dependencies"]["@colbymchenry/codegraph"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin), f"not an exact pin: {pin}"
    lock = json.loads((d / "package-lock.json").read_text())["packages"]
    top = lock["node_modules/@colbymchenry/codegraph"]
    assert top["version"] == pin and top["integrity"].startswith("sha512-")
    # the native binary is a per-platform optional package; all must be locked
    for plat in ("linux-x64", "linux-arm64", "darwin-arm64", "darwin-x64"):
        assert f"node_modules/@colbymchenry/codegraph-{plat}" in lock, plat


def test_codegraph_telemetry_and_self_download_are_off_by_default():
    init = (REPO / "aiforge_core" / "__init__.py").read_text()
    for var, off in (("CODEGRAPH_TELEMETRY", "0"), ("CODEGRAPH_NO_DOWNLOAD", "1")):
        assert f'{var}="${{{var}:-{off}}}"' in SRC, var
        assert f'"{var}", "{off}"' in init, var


def test_codegraph_is_linked_to_the_platform_launcher_not_the_npm_shim():
    """The package's `bin` is a shim that, when the per-platform package is
    missing, downloads a bundle from GitHub Releases and executes it."""
    assert "node_modules/.bin/codegraph" not in "\n".join(_code_lines())
    assert "@colbymchenry/codegraph-*/bin/codegraph" in SRC


def test_it_can_no_longer_delete_a_venv():
    """The old rebuild branches destroyed an operator's virtualenv on a network
    blip. Installing from a lock with --inexact never needs to."""
    assert "rm -rf .venv" not in SRC


def test_a_missing_prerequisite_names_the_command_for_this_os():
    assert "_install_hint" in SRC
    for manager in ("brew install", "apt install", "dnf install", "winget install"):
        assert manager in SRC, f"no hint for {manager}"


@pytest.mark.parametrize("tool", ["python", "node", "tmux"])
def test_every_os_prerequisite_has_a_hint_on_every_platform(tool):
    hint = re.search(r"_install_hint\(\) \{.*?\n\}", SRC, re.S)
    assert hint, "the hint table changed shape"
    assert f"{tool})" in hint.group(0), f"{tool} has no install hint"


# ── behaviour, with a fake uv that records every call ──────────────────────

@pytest.fixture
def synced_box(tmp_path):
    """A checkout whose .venv already imports the project (a shim onto the
    interpreter running this test) and a fake uv on PATH that logs its args
    and writes whatever `export -o` asks for. `--test` is the earliest exit
    after the install."""
    import sys
    work = tmp_path / "work"
    (work / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(RUN_SH, work / "run.sh")
    shutil.copy(REPO / "uv.lock", work / "uv.lock")
    # localhost always resolves; the repo's real index host may not, here.
    (work / "pyproject.toml").write_text(
        (REPO / "pyproject.toml").read_text().replace(
            "artifactory.internal", "localhost"))
    py = work / ".venv" / "bin" / "python"
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    log = tmp_path / "uv.log"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uv = bindir / "uv"
    uv.write_text('#!/bin/sh\n[ "$1" = --version ] && { echo "uv 0.0.0-fake"; exit 0; }\n'
                  f'echo "$*" >> "{log}"\n'
                  'if [ "$1" = export ]; then while [ $# -gt 0 ]; do\n'
                  '  [ "$1" = -o ] && { echo "x==1" > "$2"; }; shift; done; fi\n')
    uv.chmod(0o755)
    env = {**os.environ,
           "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
           "HOME": str(tmp_path / "home"),
           "AIFORGE_CONFIG_DIR": str(tmp_path / "home" / ".aiforge"),
           "AIFORGE_LM_BASE_URL": "http://127.0.0.1:9/v1",
           "AIFORGE_INSTALL_TMUX": "0", "AIFORGE_FIX_PERMS": "0",
           "AIFORGE_AUTO_MIGRATE": "0", "AIFORGE_MIGRATE_OKF": "0",
           "AIFORGE_SKIP_AIDER": "1", "AIFORGE_SKIP_INTEGRATIONS": "1"}
    for k in ("UV_DEFAULT_INDEX", "UV_INDEX_URL", "AIFORGE_EXTRAS"):
        env.pop(k, None)

    def run(**extra):
        r = subprocess.run(["bash", "run.sh", "--test"], cwd=str(work), text=True,
                           capture_output=True, timeout=120, env={**env, **extra})
        calls = log.read_text().splitlines() if log.exists() else []
        return r, [c for c in calls if c.startswith(("export ", "pip install "))]
    return run, work


def test_first_boot_installs_the_lock_and_second_boot_installs_nothing(synced_box):
    run, _ = synced_box
    r, calls = run()
    assert "AIForge connectivity test" in r.stdout, r.stdout + r.stderr
    exp, deps, local = calls[:3]
    assert exp.startswith("export --frozen") and "--extra toolchain" in exp, calls
    assert "--default-index https://localhost" in deps and "--no-build" in deps, deps
    assert "--override" in deps and "--no-config" in deps, deps
    assert "--no-deps -e ./packages/aiforge_memory -e ." in local, local
    r, again = run()
    assert again == calls, "an unchanged lock was installed again — boot needs the network"


def test_new_extras_trigger_an_install_that_includes_them(synced_box):
    run, _ = synced_box
    run()
    _, calls = run(AIFORGE_EXTRAS="crawl, embed-static")
    exports = [c for c in calls if c.startswith("export ")]
    assert "--extra crawl --extra embed-static" in exports[-1], exports


def test_an_unresolvable_index_stops_the_install_and_never_goes_public(synced_box):
    run, work = synced_box
    pp = work / "pyproject.toml"
    pp.write_text(pp.read_text().replace("localhost", "artifactory.invalid"))
    r, calls = run()
    assert r.returncode == 1
    assert "does not resolve" in r.stderr and "artifactory.invalid" in r.stderr
    assert not calls, calls
    assert "pypi.org" not in r.stdout + r.stderr


def test_an_index_the_operator_named_is_the_one_used(synced_box):
    run, _ = synced_box
    _, calls = run(UV_DEFAULT_INDEX="https://127.0.0.1/mirror/simple/")
    deps = [c for c in calls if c.startswith("pip install ")][0]
    assert "--default-index https://127.0.0.1/mirror/simple/" in deps, deps
