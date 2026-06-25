---
name: stack-run-commands
description: Know the exact build/test/run commands and where the endpoint is, per stack (Java, Python, Node/React, Go, Rust, .NET)
triggers: [run java, run python, run react, run node, spring boot, flask, fastapi, django, npm, maven, gradle, how to run, build and run, start server, mvn, uvicorn, vite, next]
source: builtin
---

For ANY coding task — bug fix, feature, POC, or new service — you must build, test, run, and hand the user how to test/use it. The `project` tool auto-detects most of this; here are the canonical commands + where the endpoint lives.

**Java — Spring Boot**
- build: `mvn -q clean package -DskipTests` (or `./gradlew build`)
- test:  `mvn -q test` (or `./gradlew test`)
- run:   `serve("mvn -q spring-boot:run", port=8080)` or `serve("java -jar target/*.jar")`
- endpoint: `http://localhost:8080` (port from `server.port` in application.yaml/properties). Try a known route or `/actuator/health`.

**Python — Flask / FastAPI / Django / script**
- deps: `pip install -r requirements.txt` (or `uv sync`)
- test: `pytest -q`
- run:  FastAPI → `serve("uvicorn main:app --port 8000", port=8000)`; Flask → `serve("flask run -p 5000", port=5000)` (or `serve("python main.py")`); Django → `serve("python manage.py runserver 8000", port=8000)`
- endpoint: `http://localhost:8000` (FastAPI also `/docs`).

**Node / React / Next / Vite**
- deps: `npm install` (or `pnpm i` / `yarn`)
- test: `npm test`
- run:  dev server → `serve("npm run dev", port=5173)` (Vite 5173, Next 3000, CRA 3000); prod → `npm run build` then `serve("npm start", port=3000)`
- endpoint: the "Local: http://localhost:PORT" the dev server prints (serve auto-detects it).

**Go**: build `go build ./...` · test `go test ./...` · run `serve("go run .", port=8080)`.
**Rust**: build `cargo build` · test `cargo test` · run `serve("cargo run")`.
**.NET**: build `dotnet build` · test `dotnet test` · run `serve("dotnet run", port=5000)`.

Rules: build before run; tests green before demo; `serve` (background) for servers, `run_command` for one-shot steps; ALWAYS finish by giving the user the URL/endpoint, a sample request, the run command, and `stop_service(pid)`.
