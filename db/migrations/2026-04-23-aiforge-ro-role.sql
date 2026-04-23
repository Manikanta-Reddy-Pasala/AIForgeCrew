-- 2026-04-23: read-only Postgres role for sql_agent (EVAL-3 result).
-- Grants SELECT on every current and future table in public to `aiforge_ro`.
-- Idempotent; safe to re-run.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aiforge_ro') THEN
    CREATE ROLE aiforge_ro LOGIN;
  END IF;
END
$$;

GRANT CONNECT ON DATABASE aiforge TO aiforge_ro;
GRANT USAGE ON SCHEMA public TO aiforge_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aiforge_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO aiforge_ro;
