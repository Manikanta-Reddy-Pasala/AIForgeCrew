-- ADK 2.0.0b1 migration — reset session storage.
--
-- Why:
--   google-adk 1.31.1 → 2.0.0b1 changed the SQLAlchemy schema for ADK
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
-- Rollback:
--   None. ADK 2.0.0b1 will recreate the session tables with its new
--   schema on the next DatabaseSessionService(db_url=...) instantiation.
--   To roll back to 1.31.1, redeploy the 1.x code; ADK 1.x will likewise
--   recreate its own tables.

BEGIN;

DROP TABLE IF EXISTS events CASCADE;
DROP TABLE IF EXISTS sessions CASCADE;
DROP TABLE IF EXISTS app_states CASCADE;
DROP TABLE IF EXISTS user_states CASCADE;
DROP TABLE IF EXISTS app_user_states CASCADE;

-- ADK 2.0b1 will auto-create its own tables on the next
-- DatabaseSessionService(db_url=...) instantiation. No CREATE TABLE here.

COMMIT;
