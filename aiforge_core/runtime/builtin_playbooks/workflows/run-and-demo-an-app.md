---
name: run-and-demo-an-app
description: After building/changing code, build→test→run it and hand the user a working endpoint
triggers: [run it, demo, poc, try it, give me the endpoint, how to run, build and run, run and test]
source: builtin
---

Whenever you write or change code (a POC, feature, or fix), PROVE it works and hand over a runnable endpoint — don't make the user ask "can you run it?".

1. **Build/compile** with the project tool or the stack's command. Fix any error before moving on.
2. **Test**: run the suite; if it's a POC with no tests, write at least one that exercises the core path. Green before you demo.
3. **Run it**: `serve(cmd, port?)` to start the app in the background. It returns the pid + the URL.
   - Backend API: serve it; note the base URL + a sample request (curl/endpoint).
   - Frontend/web: serve it; give the http://localhost:PORT to open.
   - TWO services: serve both (API first), give both URLs + how they wire together.
4. **Verify it's actually up**: hit the endpoint (web_fetch/curl the health or a known route) — confirm a real response, not just "process started".
5. **Hand off in your FINAL**: the URL(s) to open, a sample request, the exact command(s) to run it themselves, and `stop_service(pid=…)` to stop. 
6. **Clean up** on request: stop_service the pids you started.
Definition of done: it builds, tests pass, it's running, and the user has the endpoint + run/stop commands — without having to ask.
