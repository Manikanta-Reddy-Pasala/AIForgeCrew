-- LangGraph Postgres checkpointer tables.
-- Additive only — no changes to tickets, ticket_events, or memories.
-- Schema matches langgraph-checkpoint-postgres >= 2.0.0 (psycopg3 driver).

CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id         TEXT        NOT NULL,
    checkpoint_ns     TEXT        NOT NULL DEFAULT '',
    checkpoint_id     TEXT        NOT NULL,
    parent_checkpoint_id TEXT,
    type              TEXT,
    checkpoint        JSONB       NOT NULL,
    metadata          JSONB       NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id     TEXT  NOT NULL,
    checkpoint_ns TEXT  NOT NULL DEFAULT '',
    channel       TEXT  NOT NULL,
    version       TEXT  NOT NULL,
    type          TEXT  NOT NULL,
    blob          BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id     TEXT    NOT NULL,
    checkpoint_ns TEXT    NOT NULL DEFAULT '',
    checkpoint_id TEXT    NOT NULL,
    task_id       TEXT    NOT NULL,
    idx           INTEGER NOT NULL,
    channel       TEXT    NOT NULL,
    type          TEXT,
    blob          BYTEA   NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_thread_id
    ON checkpoints (thread_id);

CREATE INDEX IF NOT EXISTS idx_checkpoint_blobs_thread_id
    ON checkpoint_blobs (thread_id);
