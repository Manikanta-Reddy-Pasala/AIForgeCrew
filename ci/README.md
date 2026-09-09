# aiforge-build-image

Base image for the `build` job in [AIForgeCrew]'s `.gitlab-ci.yml`. It exists so
that job can run on a GitLab runner with no route off the estate: the image
carries the interpreter, uv, node's prerequisites and the estate CA, and the job
then fetches only what Artifactory serves.

## Build and publish

    docker build \
      --build-arg ARTIFACTORY2_PASSWORD="$ARTIFACTORY2_PASSWORD" \
      -t "$ARTIFACTORY_DOCKER_HOST/<repo>/aiforge-build:1.0.0" .
    docker push "$ARTIFACTORY_DOCKER_HOST/<repo>/aiforge-build:1.0.0"

Then in AIForgeCrew, point `BUILD_IMAGE` at that tag — in `.gitlab-ci.yml` or as
a project CI/CD variable, which overrides it.

Build args: `ARTIFACTORY2_PASSWORD` (required), `CA_URL`, `UV_VERSION`,
`PYTHON_VERSION`.

## What is in it, and why

**Python 3.12.11, and only that.** AIForgeCrew's `uv.lock` pins numpy 1.26.4,
which publishes no cp313 wheel. On 3.13 uv falls back to the numpy sdist and the
build dies with `Unknown compiler(s): [['cc'], ['gcc'], ...]`, because there is
no compiler here — and there is no compiler here because on 3.12 every
dependency in the lock resolves to a wheel. 3.13 is also worse on its own terms:
the suite loses three `net/test_ca_chain.py` real-TLS-handshake tests there.

Measured, full suite, git present and a real `.git`:

| interpreter and pin        | result                |
|----------------------------|-----------------------|
| 3.12 + numpy 1.26.4        | 1 failed, 8655 passed |
| 3.13 + numpy 2.4.6/2.5.3   | 5 failed, 8651 passed |

numpy 2 is not the problem in that second row — 3.13 is. numpy can be upgraded
on its own schedule, and this image does not depend on when.

**uv, pinned through `ARG UV_VERSION`.** Unpinned, rebuilding this image
silently changes the resolver that reads AIForgeCrew's `uv.lock`.

**git and tmux.** git because the job runs inside a repository. tmux is about
1MB and is what AIForgeCrew's persistent-shell tests need; without it they skip,
and a pty bug once hid there for exactly that reason.

**No node.** It arrives as the `nodejs-wheel-binaries` wheel, from the same
index and lock as everything else. The job writes the `npm`/`npx` shims itself —
the wheel's own `bin/npm` is broken and putting its `bin/` on `PATH` does not
fix it.

**`UV_PYTHON_DOWNLOADS=never`, set after the interpreter is installed.**
Otherwise a version miss sends uv to python-build-standalone at job time, which
on an offline runner fails slowly and obscurely instead of saying so.

**`UV_NO_CACHE` deliberately unset.** Setting it would make the job's `.uv-cache`
do nothing and rule out running with no index at all off a warm cache. Measured:
0 cache files written with it set, 6 without.

**The CA goes into both stores** — the system one via `update-ca-certificates`
and the JVM's via `keytool`. Note it is the intermediate only. That is enough
while the job runs with TLS verification off; if that changes, a trust store
holding an intermediate but no root will not verify a server that sends a bare
leaf.

## Keeping in step with AIForgeCrew

`UV_VERSION` and `PYTHON_VERSION` have to suit whatever `uv.lock` resolves.
AIForgeCrew's `before_script` runs `uv python find "$UV_PYTHON"`, so a mismatch
fails in the first seconds of the job rather than ten minutes later at the numpy
build.

## Verified

Built, then run through AIForgeCrew's entire job body in the resulting image:

- `docker build` completes; the final layer asserts python, uv, git, tmux and
  the CA alias in the JVM keystore.
- `uv sync --all-extras --dev --frozen`, sdist + wheel, node v24.19.0 and
  npm 11.17.0 through the shims.
- pytest under the job's own `--cov`/`--junitxml` flags over `tests/python/net`
  and `tests/python/llm`: 486 passed, junit.xml parsing to 486 tests and 0
  failures, valid cobertura coverage.xml. That subset holds the `test_ca_chain`
  tests that fail on 3.13.
- `npm ci` + `vite build` in 210ms, `web/dist/index.html` written.
- `uv export` 690 lines, CycloneDX SBOM 204 components, 33174 cache files.

One layer cannot be tested off the estate: the certificate fetch from
`artifactory2.internal`. The builds above substituted a locally generated
certificate for that single `curl`, so `update-ca-certificates` and `keytool`
ran for real while the fetch itself did not.

[AIForgeCrew]: ../
