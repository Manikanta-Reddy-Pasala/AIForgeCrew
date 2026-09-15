# Building the `aiforge` CLI binaries

The CLI is a thin client: it starts the sandbox and streams its events. The
engine is NOT inside it, which is why the binary is ~12 MB instead of ~2 GB.
One binary per OS, and **each one must be built on that OS** — PyInstaller
freezes the interpreter it runs on and cannot cross-compile.

The host that runs the binary needs **docker and nothing else**: no python, no
git, no node.

## One command

```bash
installer/cli/build-binary.sh                 # -> dist/cli/aiforge[.exe]
installer/cli/build-binary.sh --out /tmp/out  # somewhere else
```

It creates a throwaway venv, installs the pinned PyInstaller from
`installer/cli/pins.txt` plus this package, freezes
`packages/aiforge_cli/aiforge_cli/_entry.py`, writes the four shell-completion
scripts next to the binary, and smoke-tests the result (`--version`, `help`).
Nothing is left behind but the output directory.

Everything comes from the configured index. On a box that cannot reach the
internal Artifactory, name another one:

```bash
UV_DEFAULT_INDEX=https://pypi.org/simple installer/cli/build-binary.sh
```

## Per OS

| OS | Where | Result |
|---|---|---|
| Linux x86_64 | any Linux box (the nuc) | ~11 MB; `ldd` shows only glibc/libz/libdl/libpthread |
| Linux arm64 | an arm64 box, or x86_64 with binfmt (see CI below) | same |
| macOS | a Mac — needs python **3.11–3.13** | ~12 MB, `universal2` (x86_64 + arm64), ad-hoc signed |
| Windows | a Windows box, from **Git Bash** | `aiforge.exe` |

**Build on the oldest Linux you intend to support.** glibc is forward- but not
backward-compatible, so a binary built on 24.04 will not start on 20.04.

**macOS interpreter.** The script needs 3.11–3.13; PyInstaller 6.11 does not
support 3.14, and `/usr/bin/python3` (3.9) is too old for the package. If the
Mac's only `python3` is 3.14, point the script at a 3.12 one:

```bash
mkdir -p /tmp/pybin && ln -sf ~/.mlx/bin/python /tmp/pybin/python3   # e.g. 3.12.13
PATH=/tmp/pybin:$PATH installer/cli/build-binary.sh --out /tmp/cli-dist-mac
lipo -archs /tmp/cli-dist-mac/aiforge      # expect: x86_64 arm64
```

macOS refuses to run an unsigned arm64 binary, so the script ad-hoc signs it.
That is enough to run locally; for distribution outside the estate it needs a
Developer ID signature and notarisation.

**Windows** ships `python`, not `python3` — the script accepts either.

## In CI

`.gitlab-ci.yml` has a `package` stage:

* **`cli:linux-amd64`** — builds natively on the Linux runner. Runs on every
  pipeline; the binary and its completions are artifacts.
* **`cli:linux-arm64`** — the same build inside an `arm64` container. Needs
  either an arm64 runner or binfmt/qemu on an x86_64 one, so it is `manual` and
  `allow_failure`: a runner without binfmt should not fail the pipeline.
* **`cli:publish`** — `manual`. Uploads whatever the build jobs produced to
  Artifactory with `curl -k`.

There is no macOS or Windows job, because a Linux runner cannot produce those
binaries. Build them on a Mac and a Windows box with the commands above and
upload them with the same `curl -k` line the publish job prints.

## Uploading by hand

```bash
VER=$(sed -n 's/^version = "\(.*\)"/\1/p' packages/aiforge_cli/pyproject.toml)
curl -k -u "$ARTIFACTORY_USER:$ARTIFACTORY_TOKEN" \
     -T dist/cli/aiforge \
     "https://artifactory.internal/artifactory/generic-local/aiforge-cli/$VER/linux-amd64/aiforge"
```

`-k` skips certificate verification — fine against the internal host on a
trusted network, and the reason this is a manual step rather than something the
pipeline does silently.
