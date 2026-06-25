---
name: sql-and-orm
description: Write correct, performant SQL and use an ORM without footguns
triggers: [sql, query, orm, join, index, n+1, transaction, postgres, mysql]
source: builtin
---

- **Kill N+1**: an ORM loop that lazy-loads per row → use eager fetch / join / `IN` batch. Watch the query log.
- **Index the predicates** you filter/join/sort on; an unindexed WHERE on a big table is a full scan. But don't over-index writes.
- **Only select what you need**; avoid `SELECT *` in hot paths; paginate — never return unbounded result sets.
- **Parameterize** — never string-build SQL with input (injection + plan cache misses).
- **Transactions** for multi-statement invariants; keep them short; understand isolation level + that locks are held until commit.
- **Set-based over row-by-row**: one `UPDATE ... WHERE` beats a loop of updates.
- **Know what the ORM emits** — read the generated SQL for anything performance-sensitive.
- Migrations: see the `database-migrations` skill.
