# Generating the installers

Three artifacts, one payload. `installer/README.md` is for people who *install*
AIForge; this file is for whoever *builds* the packages.

```
dist/installer/
  aiforgecrew-<ver>-py3-none-any.whl     the app, with the web UI inside it
  aiforge_memory-<ver>-py3-none-any.whl  the vendored dependency no index carries
  lock-pins.txt                          uv.lock's versions — every first run installs them (--override)
  uv/{macos,macos-x64,linux,linux-arm64,windows}/uv[.exe]
  AIForge-<ver>.dmg        macOS
  AIForge-<ver>.msi        Windows
  aiforge_<ver>_<arch>.deb Ubuntu / Debian
```

## Step 0 — the payload (always first)

```bash
installer/build_payload.sh                      # everything, all targets
installer/build_payload.sh --target linux       # just one target's uv
```

It builds the web UI, **copies it into the package** as `aiforge_core/web_dist`,
builds the app wheel and the vendored `aiforge-memory` wheel, exports
`lock-pins.txt` and `index-url.txt`, and takes a `uv` binary per target out of
uv's wheel on the Artifactory PyPI remote (cached per version).

That UI copy is not cosmetic: the API resolves its static files from
`aiforge_core/web_dist` when installed, because the repo's `web/dist` does not
exist inside site-packages. Skip this step and the package serves a working API
and a 404 for its own UI.

Needs: `node` + `npm` (UI), `uv`, `python3` with pip. A box that has run
`./run.sh --native` has them all in `.venv/bin` — `PATH="$PWD/.venv/bin:$PATH" installer/build_payload.sh`.

Everything comes from the internal Artifactory — pyproject's index (or
`UV_DEFAULT_INDEX`) and `AIFORGE_NPM_REGISTRY` from `aiforge.env` (or
`npm_config_registry`). There is no public fallback: an index host that does not
resolve stops the build. The `uv` it packages is the `uv==<locked>` wheel, not a
GitHub release asset. `index-url.txt` carries the index into the package, since
an installed app has no pyproject and uv's own default is pypi.org.

`lock-pins.txt` (installed as `--override`, not `-c` — google-adk 2.1.0 caps
starlette <1.0 itself, so as constraints the lock is unsatisfiable) is not
optional: `override-dependencies` in pyproject (the
CVE floors — starlette, fastapi, litellm, aiohttp…) is a uv *project* setting
and never reaches wheel metadata. Without it a packaged first run resolves
fresh and got starlette 0.52.1, below the `>=1.1.0` floor.

## Step 1 — the packages

| Artifact | Command | Needs | Can I build it here? |
|---|---|---|---|
| `.dmg` | `installer/macos/build-dmg.sh` | `hdiutil` | macOS only |
| `.deb` | `installer/linux/build-deb.sh` | `dpkg-deb` | Linux, or a container |
| `.msi` | `installer/windows/build-msi.sh` | `wixl` (msitools) | **macOS or Linux** — no Windows runner needed |

```bash
make installers        # builds what this machine can, names what it skipped
```

### macOS

```bash
installer/macos/build-dmg.sh                        # Apple silicon (default)
UV_TARGET=macos-x64 installer/macos/build-dmg.sh    # Intel
```

Unsigned and un-notarised — that needs an Apple Developer ID. Gatekeeper
refuses the first open; users right-click → Open, or
`xattr -d com.apple.quarantine`. To sign in CI:

```bash
codesign --deep --force --options runtime \
         --sign "Developer ID Application: …" /Volumes/AIForge*/AIForge.app
xcrun notarytool submit AIForge-<ver>.dmg --keychain-profile … --wait
```

### Ubuntu / Debian

```bash
installer/linux/build-deb.sh                   # arch = the machine you are on
DEB_ARCH=amd64 installer/linux/build-deb.sh    # or state it
```

Architecture matters: a `.deb` whose control says `amd64` simply refuses to
install on `arm64`. Build one per architecture you ship (the payload carries
both Linux `uv` binaries).

From a Mac, use a container — which also *installs and runs* it, the only test
that means anything:

```bash
installer/verify-deb.sh                 # ubuntu:24.04 by default (22.04 needs deadsnakes)
installer/verify-deb.sh ubuntu:24.04
docker run --rm --platform linux/amd64 -v "$PWD":/src ubuntu:24.04 \
  bash -c 'apt-get update -qq && apt-get install -y -qq dpkg-dev >/dev/null &&
           cd /src && DEB_ARCH=amd64 installer/linux/build-deb.sh'
```

### Windows

```bash
brew install msitools        # macOS
apt-get install -y wixl      # Linux / CI
installer/windows/build-msi.sh
```

`wixl` writes a real MSI from a Unix host, so all three artifacts come out of
one job. What that verifies is **structure** — the File, Directory and Shortcut
tables (`msiinfo export … File`). It does not verify behaviour, and cannot:
nothing here executes Windows. Run the MSI on a Windows box before shipping it.

## Step 1b — the portable bundles

Same payload, no installer: unpack-and-run, with all state in the folder.

```bash
installer/portable/build-portable.sh --target macos            # .tar.gz
installer/portable/build-portable.sh --target linux
installer/portable/build-portable.sh --target windows          # .zip
installer/portable/build-portable.sh --target linux --offline  # + every locked wheel
```

Buildable from any host for any target — they are archives, not OS packages.
The launcher sets `AIFORGE_APP_HOME`, `AIFORGE_DATA_HOME` **and**
`AIFORGE_CONFIG_DIR` into the folder; that last one is the whole difference
between portable and installed, and leaving it out would quietly scatter a
"portable" app's memory into `~/.aiforge`.

`--offline` downloads every locked wheel (`lock-pins.txt`) for the TARGET
platform with `pip download`, and the first run installs from that folder with
`--offline --no-index`. It carries **no interpreter** — the target needs Python
3.12. Build it **on the target's OS**: pip evaluates environment markers
(`sys_platform == …`) against the host, so a cross-OS offline bundle would
silently miss platform-only packages, and the script refuses. A dependency with
no wheel for the target fails the build rather than shipping a broken bundle.

## Step 2 — proving it

```bash
make installer-verify       # build .deb → install on clean Ubuntu → GET /ui/ + /api/
```

That checks the three things packaging actually gets wrong: a dependency that
resolves on the build host but not the target, a UI that is not inside the
wheel, and a runtime the installing user cannot write. Structure checks
(`dpkg-deb -c`, `plutil -lint`, `msiinfo`) catch none of them.

## Releasing

1. Bump `version` in `pyproject.toml` — every builder reads it from there, so
   nothing else needs touching.
2. `installer/build_payload.sh`
3. Build on the hosts you have: a Linux runner gives `.deb` + `.msi`; a macOS
   runner gives `.dmg`.
4. `make installer-verify`, and run the MSI on Windows once.
5. Ship `dist/installer/*.{dmg,msi,deb}`.

An upgrade needs nothing from the user: the wheel's filename changes, the
first-run marker no longer matches, and the runtime rebuilds itself on the next
launch.

## What is deliberately NOT bundled

**A Python interpreter** — uv's managed CPython is a GitHub release asset and no
package index carries an interpreter. The target brings Python 3.12 from its own
package manager (the `.deb` depends on `python3.12`; Ubuntu 22.04 means
deadsnakes), and first run stops with the install command when it is missing.

**The venv** — built per user on first run, in their profile, not by the
installer as root. The agent runs as that user against their repos and their
`~/.aiforge`; a root-owned venv in a per-user app is a support ticket waiting to
happen.
