# ONE-38 (Gemini cloud) vs ONE-39 (Local mlx-lm) — comparison report

Same spec: `EVAL: paymentIn stats endpoint (5-file complex)` — new GET endpoint with Mongo aggregation across Controller/Service/Impl/Repo + new DTO (PaymentInStats).

## Headline

| Dimension                | ONE-38 (gemini-2.5-flash) | ONE-39 (qwen-coder-next mlx)        |
|--------------------------|----------------------------|-------------------------------------|
| Final status             | **blocked** (4× fail, escalated) | **done** (pass on attempt 3)       |
| Wall time                | 1846 s (30.8 min)          | 1107 s (18.5 min)                  |
| Doer attempts            | 4                          | 3                                  |
| Total doer turns         | 6 + 1 + 4 + 1 = **12**     | 24 + 30 + 7 = **61**               |
| Files actually changed   | **1** (only DTO, broken)   | **5** (all required files)        |
| `edit_block_ok` peak     | 1 / attempt                | 1 (last) — but cumulative across runs |
| `compile_green` ever     | **0** (always red)         | **1** on attempt 3                 |
| Feedback verdict         | fail × 4 → escalate        | fail × 2 → **pass** on 3rd        |
| PR opened                | none                       | [PR #113](https://github.com/OneShellSolutions/PosClientBackend/pull/113) — 296 +1 / 1 -1 |
| Smoke / live test        | n/a (never compiled)       | skipped (diff-only mode), green   |

## Root cause: gemini failure (NOT rate limit)

Counters show the same compile error every attempt:

```
PaymentInStats.java:[6,1] class, interface, enum, or record expected
PaymentInStats.java:[8,1] class, interface, enum, or record expected
PaymentInStats.java:[9,1] class, interface, enum, or record expected
```

Gemini wrote a malformed Lombok DTO (likely emitted only field declarations without enclosing `class { ... }` braces, or fenced markdown that leaked into the file). Across 4 retries it never repaired the syntax — kept either:
- emitting another partial overwrite of the same file (`edit_block_ok=1`, file still broken), or
- producing zero edits and giving up after 1 turn (`edit_block_ok=0`).

It also never touched the other 4 required files. So even if syntax had compiled, acceptance criteria #6 (all 5 files touched) would have failed.

Earlier theory was 429 throttling (we built the token-bucket limiter for that). True root cause is **model output quality** on a complex multi-file task; rate limiter just removed transport-level errors that were masking the real failure earlier.

## ONE-39 (local) trajectory

```
attempt 1  299s  24 turns  fail   (broken edits, mid-progress)
attempt 2  661s  30 turns  fail   (more files appearing, still red)
attempt 3  128s   7 turns  PASS   compile_green=1 / 5 files / publish_deferred
```

Local model (qwen-coder-next-MLX-4bit) needed roughly 5× the turns gemini got, but each turn produced incremental valid edits. The accumulation across attempts (worktree state survives) is what got it across the line — turn budget per attempt was the right escape hatch.

PR #113 file deltas:
- `PaymentInController.java`           +44 -1
- `PaymentInService.java`               +15
- `PaymentInServiceImpl.java`           +22
- `PaymentInCustomRepository.java`      +181
- `model/PaymentInStats.java`           +34

All 5 required files, only `feature/paymentin/` touched (criterion #7), Mongo aggregation in repo, Lombok @Data DTO.

## Quality scorecard (PR #113)

| AC | Requirement                                                    | Status |
|----|-----------------------------------------------------------------|--------|
| 1  | Endpoint `/v1/api/paymentIn/stats-eval/{businessId}/{yyyyMM}`   | ✅ — controller @GetMapping |
| 2  | `Mono<PaymentInStats>` with 8 fields                            | ✅ — DTO has 8 Lombok fields |
| 3  | Excludes soft-deleted (`deleted!=true`)                         | ◯ — needs spot-check in repo aggregation |
| 4  | `log.info` on entry with businessId+month                       | ◯ — needs spot-check |
| 5  | `mvn -DskipTests compile` passes                                | ✅ — `compile_green=1` recorded |
| 6  | All 5 files touched                                             | ✅ |
| 7  | No changes outside `feature/paymentin/`                         | ✅ — diff confined |

(◯ = not auto-asserted by gate; eyeball before merge.)

## Takeaways

1. Cloud blocked path was a model-quality regression on a 5-file Java task, not infra. Token-bucket limiter (commit `9c176dc`) is still right but not the fix here.
2. Local 4-bit qwen-coder-next handles the same spec at ~3× wall-time but reliably converges. 3 attempts is the right `max_attempts` for this complexity tier.
3. Worktree-survives-attempts is load-bearing: ONE-39 attempt 3 won partly because attempts 1+2 already laid groundwork. Don't reset worktree between attempts.
4. Gemini retry on the SAME bad DTO suggests the doer prompt isn't surfacing the prior compile error strongly enough. Targeted-fixlist feedback was added; verify it actually reaches gemini's next-prompt with the malformed-file diff inlined.
5. Re-run ONE-38 on gemini after the limiter fix to confirm whether it's transport (429) or capability (DTO syntax). My read: capability, not transport.

## Suggested follow-ups

- ONE-40: re-run ONE-38 spec on gemini-2.5-flash with rate-limiter active — confirm/refute capability theory.
- ONE-41: add a "DTO-specific" turn template (Lombok skeleton + javac snippet) when the failing file is a `*Stats|*Dto|*Request|*Response.java` and prior compile error matches "class, interface, enum, or record expected".
- ONE-42: in feedback-gate fail comment, embed the FULL contents of the broken file in the next-attempt prompt (not just the compile error). Gemini was overwriting blind.
