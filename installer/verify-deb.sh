#!/usr/bin/env bash
# The only test that proves a package works: install it on a CLEAN OS and hit
# the API it serves.
#
# Builds the .deb inside an Ubuntu container (so the arch matches the container,
# not the build host), installs it as a normal user, lets the first run
# provision its own CPython, starts the server and asks it for the UI and an API
# endpoint. Everything the packaging can get wrong — a missing dependency, a
# wheel that cannot resolve, a UI that is not inside the wheel — shows up here
# and nowhere else.
#
#   installer/verify-deb.sh [ubuntu:22.04]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${1:-ubuntu:22.04}"

[[ -n "$(ls -1 "$REPO_ROOT"/dist/installer/aiforgecrew-*.whl 2>/dev/null || true)" ]] || {
  echo "no payload — run installer/build_payload.sh first" >&2; exit 1; }

exec docker run --rm -v "$REPO_ROOT":/src "$IMAGE" bash -euo pipefail -c '
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends \
      dpkg-dev ca-certificates curl git tmux >/dev/null
  cd /src
  echo "==> build"
  bash installer/linux/build-deb.sh >/dev/null
  DEB="$(ls -1 dist/installer/aiforge_*_$(dpkg --print-architecture).deb | head -1)"
  echo "==> install $DEB"
  dpkg -i "$DEB" >/dev/null

  # As a NORMAL user: the per-user runtime is the whole design, and root would
  # hide a permissions mistake in it.
  useradd -m tester
  echo "==> first run (provisions CPython + the venv)"
  su tester -c "nohup aiforge --no-runner --no-sync >/tmp/aiforge.log 2>&1 &"
  for _ in $(seq 1 90); do
    curl -sf http://127.0.0.1:8799/ui/ >/dev/null 2>&1 && break
    sleep 2
  done

  fail=0
  code="$(curl -s -o /tmp/ui.html -w "%{http_code}" http://127.0.0.1:8799/ui/ || true)"
  # The UI must come from INSIDE the wheel: ../../web/dist does not exist in
  # site-packages, and a 404 here is the packaged-app failure nobody notices
  # until a user opens it.
  if [ "$code" = "200" ] && grep -q "<!doctype html>" /tmp/ui.html; then
    echo "    ok   /ui/            200, real HTML from the wheel"
  else
    echo "    FAIL /ui/            $code"; fail=1
  fi
  code="$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8799/api/runtime/llm_backend || true)"
  if [ "$code" = "200" ]; then echo "    ok   /api/…          200"
  else echo "    FAIL /api/…          $code"; fail=1; fi

  su tester -c "test -x ~/.local/share/aiforge/venv/bin/aiforge" \
    && echo "    ok   runtime         per-user venv" \
    || { echo "    FAIL runtime"; fail=1; }

  pkill -f aiforge >/dev/null 2>&1 || true
  [ "$fail" = 0 ] || { echo; echo "--- log ---"; tail -30 /tmp/aiforge.log; exit 1; }
  echo "==> package verified on '"$IMAGE"'"
'
