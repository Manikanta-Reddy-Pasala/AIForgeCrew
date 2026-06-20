# Migrating the bare NUC stack → docker-compose (no data loss)

The existing NUC runs a mix: **bare** Postgres + API + graph runner +
python embed/rerank sidecars, plus a **containerised** Neo4j. This brings
the whole thing under one `docker compose` while preserving all data.

Strategy: Postgres is **dump+restored** into a compose volume (the bare
cluster is left intact as rollback). Neo4j **reuses its existing data
volume** (external), so the graph carries over with zero copying.

Run everything from the repo root on the NUC. `DOCKER="sudo docker"` if
your user isn't in the `docker` group.

## 0. Back up first (always)

```bash
scripts/compose/backup-prod.sh ~/aiforge-backups
# → pg-aiforge-<ts>.sql.gz  +  neo4j-data-<ts>.tar.gz
```

## 1. Find the existing Neo4j data volume

```bash
sudo docker volume ls | grep -i neo4j      # e.g. graph_rag_neo4j_data
export NEO4J_VOLUME=graph_rag_neo4j_data    # the compose file defaults to this
```

If you'd rather start a fresh graph instead of reusing it:
`export NEO4J_VOLUME_EXTERNAL=false`.

## 2. Stop the bare services (keep the Neo4j container for now)

```bash
# API + graph runner + python sidecars (adjust to how yours were started)
pkill -f 'uvicorn aiforge_core.api.api:app' || true
pkill -f 'aiforge_core.runtime.adk_runner'  || true
pkill -f 'venv-embed/bin/uvicorn'           || true
pkill -f 'venv-rerank/bin/uvicorn'          || true
# Stop bare Postgres so the compose one can take :5432
sudo systemctl stop postgresql@16-main

# Free the existing Neo4j container so compose can adopt the volume
# under its own service (data stays in NEO4J_VOLUME).
sudo docker rm -f aiforge-neo4j || true
```

## 3. Bring up the stack (Postgres + Neo4j first)

```bash
sudo docker compose up -d --build postgres neo4j embed rerank
```

## 4. Restore the Postgres data

```bash
scripts/compose/restore-pg.sh ~/aiforge-backups/pg-aiforge-<ts>.sql.gz
# prints the ticket count — confirm it matches the bare cluster (e.g. 104)
```

## 5. Start the app + runner

```bash
sudo docker compose up -d --build api runner
sudo docker compose ps
```

## 6. Verify

```bash
curl -s localhost:8799/api/health          # storage should be postgres-backed
curl -s localhost:8799/api/tickets | head  # tickets present
```

The UI is at http://<nuc>:8799/ui/.

## Rollback

```bash
sudo docker compose down                    # volumes are kept
sudo systemctl start postgresql@16-main     # bare Postgres back
# restart your bare API/runner/sidecars and the old Neo4j container
```

Nothing here drops a volume or the bare Postgres cluster, so the
pre-migration state is always recoverable.
