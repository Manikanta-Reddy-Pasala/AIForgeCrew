-- Ticket store in aiforge Postgres.
-- Run:  PGPASSWORD=... psql -h 127.0.0.1 -U manikanta aiforge -f this.sql

CREATE TABLE IF NOT EXISTS tickets (
  id            bigserial PRIMARY KEY,
  identifier    text UNIQUE NOT NULL,          -- ONE-<n>
  title         text NOT NULL,
  body          text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'todo',  -- todo|in_progress|in_review|done|blocked|cancelled
  priority      text NOT NULL DEFAULT 'medium',-- low|medium|high|urgent
  assignee_role text,                           -- architect|sr_developer|developer|fact_extract
  parent_id     bigint REFERENCES tickets(id) ON DELETE CASCADE,
  branch        text,                           -- aiforge/ONE-<parent>-<slug>
  project       text,
  labels        text[] NOT NULL DEFAULT '{}',
  metadata      jsonb  NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz
);
CREATE INDEX IF NOT EXISTS tickets_assignee_status ON tickets(assignee_role, status);
CREATE INDEX IF NOT EXISTS tickets_parent ON tickets(parent_id);
CREATE INDEX IF NOT EXISTS tickets_status ON tickets(status);

CREATE TABLE IF NOT EXISTS ticket_events (
  id         bigserial PRIMARY KEY,
  ticket_id  bigint NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  agent_role text,
  kind       text NOT NULL,              -- comment|status_change|tool_call|error|llm_turn|retain|child_created
  body       text,
  metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ticket_events_ticket_ts ON ticket_events(ticket_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ticket_events_kind ON ticket_events(kind);

-- Counter for ONE-<n> identifier generation.
CREATE TABLE IF NOT EXISTS ticket_counter (singleton boolean PRIMARY KEY DEFAULT TRUE, next_n bigint NOT NULL);
INSERT INTO ticket_counter (singleton, next_n) VALUES (TRUE, 100) ON CONFLICT DO NOTHING;

-- updated_at trigger
CREATE OR REPLACE FUNCTION tickets_touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

DROP TRIGGER IF EXISTS tickets_updated_at ON tickets;
CREATE TRIGGER tickets_updated_at BEFORE UPDATE ON tickets
  FOR EACH ROW EXECUTE FUNCTION tickets_touch_updated_at();
