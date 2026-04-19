#!/usr/bin/env bash
# scripts/patch-hindsight-shutdown-bug.sh — patch the Hermes Hindsight plugin
# to survive interpreter shutdown.
#
# Symptom:
#   RuntimeError: cannot schedule new futures after interpreter shutdown
#   at plugins/memory/hindsight/__init__.py: _run_sync → aiohttp → asyncio.to_thread
#
# Root cause: the plugin holds a long-lived event loop in a daemon thread.
# aiohttp internally calls asyncio.to_thread which submits to Python's default
# executor. At interpreter teardown, that executor is closed by
# concurrent.futures._python_exit. Daemon loop that wakes up afterward tries
# to submit a new future → crash, which kills the subprocess with exit=null
# and propagates a KeyboardInterrupt up the Hermes CLI signal handler chain.
#
# Fix:
#   1. Wrap _run_sync to catch RuntimeError containing 'schedule new futures'
#      and return None silently (log at debug).
#   2. Register atexit handler that stops the background loop BEFORE Python's
#      _python_exit runs, so no in-flight coroutines see the dead executor.
#   3. Set auto_retain=false in config.json as defense-in-depth (retain via
#      explicit tool call only, never on session-end atexit path).
#
# Idempotent — patches tagged with BEGIN/END markers, re-applies on re-run.
set -euo pipefail

[[ "$(uname -s)" == "Darwin" ]] || { echo "macOS only" >&2; exit 1; }

PLUGIN="$HOME/.hermes/hermes-agent/plugins/memory/hindsight/__init__.py"
[[ -f "$PLUGIN" ]] || { echo "plugin not found at $PLUGIN" >&2; exit 1; }

MARKER_BEGIN="# BEGIN aiforge-shutdown-patch"
MARKER_END="# END aiforge-shutdown-patch"

# Remove any prior patch block first.
if grep -q "$MARKER_BEGIN" "$PLUGIN"; then
  echo ">>> removing prior patch block"
  ~/.hermes/hermes-agent/venv/bin/python3 - "$PLUGIN" <<'PY'
import sys, re
p = sys.argv[1]
with open(p) as f: text = f.read()
pat = re.compile(r"# BEGIN aiforge-shutdown-patch.*?# END aiforge-shutdown-patch\n", re.DOTALL)
with open(p, "w") as f: f.write(pat.sub("", text))
PY
fi

# Locate the existing _run_sync definition + replace its body with a guarded version.
~/.hermes/hermes-agent/venv/bin/python3 - "$PLUGIN" <<'PY'
import re, sys
from pathlib import Path
p = Path(sys.argv[1])
txt = p.read_text()

OLD = '''def _run_sync(coro, timeout: float = 120.0):
    """Schedule *coro* on the shared loop and block until done."""
    loop = _get_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)'''

NEW = '''# BEGIN aiforge-shutdown-patch
_shutting_down = False

def _run_sync(coro, timeout: float = 120.0):
    """Schedule *coro* on the shared loop and block until done.

    Patched: swallow interpreter-shutdown RuntimeError so a background retain
    doesn't crash the hermes subprocess at exit. Returns None on shutdown.
    """
    global _shutting_down
    if _shutting_down:
        logger.debug("hindsight _run_sync called during shutdown — ignoring")
        try: coro.close()
        except Exception: pass
        return None
    try:
        loop = _get_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)
    except RuntimeError as e:
        if "schedule new futures" in str(e) or "interpreter shutdown" in str(e):
            logger.warning("hindsight _run_sync: interpreter shutting down, dropping coroutine: %s", e)
            try: coro.close()
            except Exception: pass
            return None
        raise
    except Exception as e:
        # aiohttp-raised variants (ClientConnectorError, etc.) during teardown
        if _shutting_down:
            logger.warning("hindsight _run_sync (post-shutdown): %s", e)
            return None
        raise


def _stop_loop_on_exit():
    """atexit hook: stop the background loop BEFORE Python's _python_exit kills
    the default executor so in-flight coroutines don't hit the dead executor."""
    global _shutting_down, _loop
    _shutting_down = True
    try:
        if _loop is not None and _loop.is_running():
            _loop.call_soon_threadsafe(_loop.stop)
    except Exception:
        pass

import atexit
atexit.register(_stop_loop_on_exit)
# END aiforge-shutdown-patch'''

if OLD not in txt:
    print("!! could not find original _run_sync block — aborting. Inspect the file manually.")
    sys.exit(2)

p.write_text(txt.replace(OLD, NEW))
print("patched _run_sync + atexit hook in", p)
PY

# Defense-in-depth: set auto_retain=false in config.json (explicit retain only).
CFG="$HOME/.hermes/hindsight/config.json"
if [[ -f "$CFG" ]]; then
  ~/.hermes/hermes-agent/venv/bin/python3 - "$CFG" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
cfg = json.loads(p.read_text())
# Re-enable auto_retain now that the shutdown bug is patched.
cfg["auto_retain"] = True
cfg["retain_async"] = False  # keep sync path (more deterministic, avoids 2nd executor)
p.write_text(json.dumps(cfg, indent=2))
print("reset auto_retain=True, retain_async=False in", p)
PY
fi

echo
echo "patched. Next Sr Dev run will use the guarded _run_sync."
