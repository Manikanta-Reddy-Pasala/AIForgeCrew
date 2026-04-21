"""AIForge v5 orchestrator runtime.

Single-Postgres tick-based orchestrator. One tick per role per launchd
timer firing. Per-role fcntl lock, claim_next → tool loop → finalize.
"""
