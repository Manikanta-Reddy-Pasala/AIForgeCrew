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

## Moving this to its own repo

The file uses no build context — no `COPY`, nothing read from the working
directory — so it drops into a standalone repo unchanged. Build it there, push
the tag to Artifactory, and point `BUILD_IMAGE` in AIForgeCrew's
`.gitlab-ci.yml` at it. Keep `UV_VERSION` and `PYTHON_VERSION_AIFORGE` in step
with what AIForgeCrew's `uv.lock` needs; the job asserts the interpreter is
there with `uv python find "$UV_PYTHON"` in before_script, so a mismatch fails
in the first seconds rather than at the numpy build.

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

The image itself was BUILT and then run through the whole job body:

- `docker build` completes all 17 steps. Both interpreters install,
  `update-ca-certificates` and `keytool` run, and `python3` still resolves to
  3.13.7, so nothing else using this image sees a different default.
- In the built image: `uv python find 3.12`, a 3.12.11 venv, `uv sync
  --all-extras --dev --frozen`, sdist + wheel, the node shims (node v24.19.0,
  npm 11.17.0), tmux 3.4 and git 2.43.0 on PATH.
- pytest with the job's own --cov/--junitxml flags over tests/python/net and
  tests/python/llm: 486 passed, junit.xml parses to 486 tests / 0 failures,
  coverage.xml is valid cobertura. That subset includes the test_ca_chain tests
  that fail on 3.13.
- `npm ci` + `vite build` in 210ms, web/dist/index.html written.
- `uv export` 690 lines, CycloneDX SBOM 204 components.
- With UV_NO_CACHE=false the cache fills: 33174 files.

One layer cannot be tested outside your network: the certificate is fetched
from `artifactory2.internal`. The build above substituted a locally generated
certificate for that one `curl`, so `update-ca-certificates` and `keytool` were
exercised for real while the fetch itself was not.
