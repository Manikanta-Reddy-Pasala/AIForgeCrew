---
name: database-migrations
description: Change a schema safely without downtime or data loss
triggers: [migration, schema, database, alter table, ddl, sql, backfill]
source: builtin
---

Schema changes are one-way and risky. Make them additive, reversible, and backward-compatible.

- **Use the project's migration tool** (don't hand-edit prod schema). One migration per logical change; check it into version control.
- **Expand → migrate → contract:** add the new column/table (nullable/with default) first; backfill data in batches; switch reads/writes; only later drop the old column — never all at once.
- **Backward compatible with the running app**: the old code must work during the rollout window.
- **Backfill in batches** (not one giant UPDATE) to avoid long locks; watch lock/replication.
- **Always write the down/rollback** (or a forward-fix plan). Test the migration on a copy of real-shaped data first.
- Big tables: beware locking DDL — use online/concurrent variants (e.g. `CREATE INDEX CONCURRENTLY`).
