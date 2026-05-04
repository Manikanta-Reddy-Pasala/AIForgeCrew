-- ADK 2.0.0b1 migration — reset session storage.
--
-- Why:
--   google-adk 1.x → 2.0.0b1 changed the SQLAlchemy schema for ADK
--   sessions (see google.adk.sessions.schemas v0 → v1). The tables
--   the 1.x DatabaseSessionService created cannot be reused as-is.
--
-- Apply where:
--   On the NUC server hosting the AIForge Postgres instance, against the
--   `AIFORGE_DSN` database. NOT applicable on developer laptops unless
--   they also run a local aiforge Postgres.
--
-- Apply how (NUC):
--   ssh nuc 'psql "$AIFORGE_DSN" -1 -f /path/to/this/file.sql'
--
-- Data loss:
--   Yes — by design. Sessions hold scratch state for in-flight ADK runs.
--   Tickets (`tickets`, `ticket_events`, `hitl_pending`) are NOT touched.
--   Schedule the apply during a maintenance window with no in-flight
--   tickets, or accept that any in-flight run will be re-claimed and
--   re-run from scratch.
--
-- Rollback procedure (no SQL script — manual steps):
--   Redeploy the prior ADK 1.x code. ADK 1.x will recreate its own v0 tables on
--   the next DatabaseSessionService(db_url=...) instantiation. Any leftover
--   `adk_internal_metadata` row is harmless to 1.x — it doesn't read it.

-- Operator-error guard: refuse to run against the wrong database.
DO $$
BEGIN
  IF current_database() NOT IN ('aiforge', 'aiforge_dev') THEN
    RAISE EXCEPTION
      'Wrong database: %. This migration must run against the aiforge (or aiforge_dev) database.',
      current_database();
  END IF;
END
$$;

BEGIN;

-- Tables defined by google.adk.sessions.schemas v0 (ADK 1.x) and v1 (ADK 2.0.0b1).
-- DROP order: child first, then parent — keeps the script correct even if
-- a future edit removes the CASCADE clauses. `events` has an FK to
-- `sessions`; the rest are independent.
DROP TABLE IF EXISTS events CASCADE;
DROP TABLE IF EXISTS sessions CASCADE;
DROP TABLE IF EXISTS app_states CASCADE;
DROP TABLE IF EXISTS user_states CASCADE;
DROP TABLE IF EXISTS adk_internal_metadata CASCADE;

-- ADK 2.0.0b1 will auto-create its own tables on the next
-- DatabaseSessionService(db_url=...) instantiation. No CREATE TABLE here.

COMMIT;
