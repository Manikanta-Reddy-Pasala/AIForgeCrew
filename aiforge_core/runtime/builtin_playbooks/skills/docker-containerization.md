---
name: docker-containerization
description: Containerize and run apps with sane, small, secure images
triggers: [docker, dockerfile, container, image, compose, kubernetes]
source: builtin
---

- **Small base** (slim/alpine/distroless). Pin versions; don't use `latest` in production images.
- **Layer for cache**: copy dependency manifests and install deps BEFORE copying source, so code changes don't bust the dep layer.
- **Multi-stage build**: compile/install in a build stage, copy only artifacts into a minimal runtime stage.
- **Non-root user**, read-only FS where possible, drop capabilities. Never bake secrets into layers — pass at runtime (env/secret mount).
- **.dockerignore** the .git, node_modules, build dirs to keep context small.
- **One concern per container**; healthcheck defined; logs to stdout/stderr.
- Verify: `docker build` succeeds, image size is reasonable, container starts and serves.
