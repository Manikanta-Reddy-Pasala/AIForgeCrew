---
name: containerize-an-app
description: Procedure to put an app in a small, secure container
triggers: [containerize, dockerize, docker image, build container, compose]
source: builtin
---

1. **Identify** runtime, entrypoint, ports, required env/secrets, build artifacts.
2. **Write the Dockerfile** (see `docker-containerization` skill): minimal pinned base, multi-stage (build → slim runtime), deps before source for cache, non-root user.
3. **.dockerignore** the noise (.git, deps dir, build output).
4. **Build + run locally**: `docker build`, then run and hit the app; confirm it starts, serves, logs to stdout, and respects env config.
5. **Healthcheck** + sane resource limits; secrets passed at runtime, not baked in.
6. **Compose** (if multi-service) wiring deps (db/cache); document the run command.
7. Verify image size is reasonable and the container runs as non-root.
