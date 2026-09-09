# Build image for the AIForgeCrew CI job

Build:

    docker build -f ci/build-image.Dockerfile \
      --build-arg ARTIFACTORY2_PASSWORD=... \
      -t $ARTIFACTORY_DOCKER_HOST/<repo>/aiforge-build:1.0.0 .

Then set `BUILD_IMAGE` in `.gitlab-ci.yml` (or as a project CI/CD variable) to
that tag.

## The five changes to the original Dockerfile

**1 — Python 3.12.11 alongside 3.13.7.** `uv.lock` pins `numpy==1.26.4`, which
publishes no cp313 wheel. On 3.13 uv falls back to the numpy sdist and the build
dies with `Unknown compiler(s): [['cc'], ['gcc'], ...]`, because
`build-essential` is deliberately not in the image. 3.13 stays the default and
`python3` still points at it, so nothing else that uses this image changes; the
job selects the other one with `UV_PYTHON=3.12`.

The alternative is upgrading numpy: `uv lock --upgrade-package numpy` resolves
to 2.4.6/2.5.3 and syncs clean on 3.13. Measured against a 3.12 baseline, the
full suite says numpy 2 is not the problem — 3.13 is:

| run                         | result                  |
|-----------------------------|-------------------------|
| 3.12 + numpy 1.26.4         | 1 failed, 8655 passed   |
| 3.13 + numpy 2.4.6/2.5.3    | 5 failed, 8651 passed   |

The four extra failures are `net/test_ca_chain.py` (three real-TLS-handshake
tests, a 3.13 ssl behaviour change) and one flaky multiprocess timing test.
`test_a_missing_uv_never_reaches_astral_sh` fails in BOTH runs and so is
pre-existing, unrelated to either change.

So numpy could be upgraded on its own merits. It is 3.12 that keeps the CA-chain
tests passing, which is the reason to select it.

**2 — `git` and `tmux`.** git because the job runs inside a repository. tmux is
about 1MB and is what the persistent-shell tests need; without it they skip, and
a pty bug once hid there for exactly that reason.

**3 — uv pinned via `ARG UV_VERSION`.** Unpinned, a rebuild of this image
silently changes the resolver that reads `uv.lock`.

**4 — `ENV UV_NO_CACHE=true` removed.** It makes GitLab's `.uv-cache` cache do
nothing and rules out the warm-cache air-gap fallback. Measured: with it set a
package install writes 0 cache files, without it 6. The job can still set it.

**5 — `UV_PYTHON_DOWNLOADS=never`, after the interpreters are installed.**
Otherwise a version miss makes uv reach for python-build-standalone at job time,
which on an offline runner fails slowly and obscurely instead of saying so.

Also fixed in passing: `keytool -alias -infra_intermediate` set the alias to the
literal string `-infra_intermediate`.

## Two things to check on your side

`ARTIFACTORY_USERNAME` and `ARTIFACTORY_PASSWORD` are declared as build args and
never used — only `ARTIFACTORY2_PASSWORD` is.

Only the intermediate certificate is imported, not the root. That is enough
while the job runs with TLS verification off. If you turn verification back on,
a trust store holding an intermediate but no root does not verify a server that
sends a bare leaf.

## Verified, on a real container

- `python:3.12-slim`, `ubuntu:24.04`, and this layout were each run end to end.
- `UV_PYTHON=3.12` with `UV_PYTHON_DOWNLOADS=never`: `uv sync --all-extras --dev
  --frozen` exits 0 on 3.12.11 with both interpreters present.
- The same sync on 3.13.7 fails on numpy 1.26.4 with no compiler.
- Both suite runs above were done with git installed and a real .git present.
  An earlier run without them reported 97 failures that were all
  git-shelling-out tests, and meant nothing.
- `apt-get dist-clean` is a real subcommand on Ubuntu 24.04's apt.
- tmux 3.4 and git 2.43.0 come from the 24.04 archive.
