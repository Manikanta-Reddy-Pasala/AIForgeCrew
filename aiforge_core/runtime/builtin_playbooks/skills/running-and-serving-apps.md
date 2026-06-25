---
name: running-and-serving-apps
description: Run/serve an app in the background, give the endpoint, and stop it cleanly
triggers: [run, serve, start, localhost, endpoint, port, stop, background, server]
source: builtin
---

A server runs forever — DON'T start it with a blocking command (run_command/project run would hang). Use the `serve` tool.

- **Start in background**: `serve(cmd, port?)` launches it detached, watches the startup log for the bound URL/port, and returns `{pid, url}` immediately. Pass a `port` hint when the framework doesn't print a URL.
- **Give the user what they need**: the exact URL to open (e.g. http://localhost:5173), the command you ran, and how to stop it — `stop_service(pid)`.
- **Two services** (API + web UI): `serve` each; give BOTH URLs and note how they connect (e.g. UI proxies /api to the API port). Start the API first.
- **Auto-cleanup**: a served process auto-stops after AIFORGE_SERVE_TTL_S (default 30 min) if you forget, and all are killed when the app process exits — pass ttl_s to override per call.
- **Stop**: `stop_service(pid)` kills the whole process group; `list_services()` shows what's still alive. The chat Stop button also kills everything you started.
- **Before serving**: build + run tests first (a broken build shouldn't be "run"). If `serve` reports the process exited on startup, read the returned log_tail, fix, retry.
- Use `run_command` for one-shot steps (install/build/test); `serve` only for long-lived processes.
