---
name: java-spring-boot
description: Write idiomatic Java + Spring Boot services
triggers: [java, spring, spring boot, jpa, bean, controller, maven, gradle]
source: builtin
---

- **Layering**: Controller (HTTP only) → Service (business logic) → Repository (data). Keep logic out of controllers.
- **Constructor injection** (not field `@Autowired`); makes beans testable + final fields.
- **DTOs at the boundary**: don't expose JPA entities directly; map to/from request/response objects. Validate with `@Valid` + Bean Validation.
- **Transactions**: `@Transactional` at the service boundary; understand propagation + that it only applies via the proxy (no self-invocation).
- **Null safety**: `Optional` for "might be absent" returns; avoid returning null collections (return empty).
- **Streams** for transforms, but don't force them where a loop is clearer; mind performance on hot paths.
- **Config** via `application.yaml` + `@ConfigurationProperties`, not scattered `@Value`. Profiles for env differences.
- Build/test: `mvn`/`gradle` clean verify; write `@SpringBootTest`/slice tests.
