---
name: write-database-migration
description: Procedure to ship a schema change safely
triggers: [database migration, schema change, alter table, add column, migrate db]
source: builtin
---

1. **Plan the expand→contract** path (see `database-migrations` skill). Will the running app tolerate the intermediate state?
2. **Generate the migration** with the project's tool; one logical change; include the down/rollback.
3. **Additive first**: add nullable column / new table; deploy; app writes both old+new if needed.
4. **Backfill** existing rows in batches; verify counts.
5. **Switch** reads/writes to the new shape once backfilled; deploy.
6. **Contract**: in a LATER migration, drop the old column/constraint — never in the same release as the add.
7. **Test** the migration (up AND down) on a copy of real-shaped data; watch locks on big tables (use concurrent DDL).
