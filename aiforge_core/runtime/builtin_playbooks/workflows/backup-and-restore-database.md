---
name: backup-and-restore-database
description: Procedure to back up a database and verify you can restore it
triggers: [backup, restore, disaster recovery, dump, pg_dump, snapshot, data loss]
source: builtin
---

A backup you've never restored is not a backup.

1. **Pick the method**: logical dump (`pg_dump`/`mysqldump`) for portability, or snapshot/PITR for large/critical DBs.
2. **Automate + schedule** (cron/managed) with retention; store OFF the primary host (and ideally off-region/encrypted).
3. **Capture consistently**: dump in a transaction / use snapshot so you don't get a torn state.
4. **TEST RESTORE regularly** into a scratch instance — measure the restore time (your real RTO) and verify data integrity.
5. **Document the runbook**: exact restore commands, where backups live, who to call.
6. **Before a risky migration/deploy**: take a fresh backup AND confirm it restores.
7. Monitor backup success; alert on a missed/failed backup.
