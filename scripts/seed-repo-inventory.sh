#!/usr/bin/env bash
# scripts/seed-repo-inventory.sh — direct SQL seed of OneShell codeRepo
# inventory into Hindsight aiforge bank. Bypasses LLM extraction (avoids
# NIM rate-limits + LM Studio `response_format.type=json_object` rejection).
#
# Idempotent: uses INSERT ... ON CONFLICT DO UPDATE keyed by (bank_id, chunk_id).
# Tag: `repo-inventory`.
# Source of truth: docs/repo-inventory.md (ONE-54).
set -euo pipefail

SSH_HOST="${SSH_HOST:-manikanta@192.168.70.185}"
LOCAL_ONLY="${LOCAL_ONLY:-0}"

run_psql() {
  if [[ "$LOCAL_ONLY" == "1" ]]; then
    PGPASSWORD=hindsight psql -h 127.0.0.1 -p 5433 -U hindsight -d hindsight -At -c "$1"
  else
    ssh "$SSH_HOST" "PGPASSWORD=hindsight ~/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 5433 -U hindsight -d hindsight -At -c \"$1\""
  fi
}

copy_sql() {
  local sql_file="$1"
  if [[ "$LOCAL_ONLY" == "1" ]]; then
    PGPASSWORD=hindsight psql -h 127.0.0.1 -p 5433 -U hindsight -d hindsight -f "$sql_file"
  else
    scp -q "$sql_file" "$SSH_HOST:/tmp/$(basename "$sql_file")"
    ssh "$SSH_HOST" "PGPASSWORD=hindsight ~/.pg0/installation/18.1.0/bin/psql -h 127.0.0.1 -p 5433 -U hindsight -d hindsight -f /tmp/$(basename "$sql_file")"
  fi
}

TMPSQL=$(mktemp /tmp/seed-repo-inventory.XXXX.sql)
trap "rm -f $TMPSQL" EXIT

cat > "$TMPSQL" <<'SQL'
-- ONE-54: seed repo inventory as world facts in aiforge bank
-- Idempotent: chunk_id = 'repo-inventory:<repo-name>'; upsert on conflict

BEGIN;

-- Ensure aiforge bank exists (no-op if already present)
INSERT INTO banks (bank_id, name) VALUES ('aiforge', 'AIForgeCrew')
  ON CONFLICT (bank_id) DO NOTHING;

-- Remove previous inventory seed (clean re-seed semantics)
DELETE FROM memory_units WHERE bank_id='aiforge' AND 'repo-inventory' = ANY(tags);

-- Insert one fact per repo
INSERT INTO memory_units (bank_id, text, context, fact_type, metadata, tags) VALUES
SQL

# One-liner per repo: (category, language, status, purpose). Matches docs/repo-inventory.md.
# Format: "<repo>|<cat>|<lang>|<status>|<purpose>"
while IFS='|' read -r repo cat lang status purpose; do
  [[ -z "$repo" ]] && continue
  # SQL-escape single quotes in purpose
  esc_purpose=${purpose//\'/\'\'}
  text="OneShell codeRepo: $repo — category=$cat, language=$lang, status=$status. $esc_purpose"
  cat >> "$TMPSQL" <<EOF
  ('aiforge', '$text', 'repo-inventory', 'world',
   '{"repo":"$repo","category":"$cat","language":"$lang","status":"$status","source":"ONE-54"}',
   ARRAY['repo-inventory','codeRepo','$cat','$status']),
EOF
done <<'EOF'
MongoDbService|core|java|active|Mandatory MongoDB gateway — all services query via this; never direct Mongo from elsewhere. Port 8080.
PosClientBackend|core|java|active|Main client API plus GraphQL. Docker + K8s pos namespace. Port 8090. Push-syncs via NATS business.push.request to PosServerBackend.
PosServerBackend|core|java|active|Cloud backend. Receives Docker sync, runs Mongo change streams, applies TransactionSyncRules. Port 8091.
GatewayService|core|java|active|API Gateway + JWT auth. Spring Boot 2.7/Java 17. Port 8080.
BusinessService|core|java|active|Business logic for PosAdmin + mobile clients. Spring Boot 2.7/Java 21. Port 8080.
PosService|core|java|active|POS operations service. Java 21. Port 8081.
Scheduler|core|java|active|Recurring events + invoice scheduling. Spring Boot 3.2/Java 21. Port 8080.
QuartzScheduler|core|java|active|Stock/balance corrections + batch jobs via Quartz. Spring Boot 3.2/Java 21. Port 8080.
EmailService|support|java|active|Email send + template service.
NotificationService|support|java|active|Push + in-app notifications.
WhatsappApiService|support|java|maintenance|WhatsApp Business API integration. Last commit Feb 2026.
GstApiService|support|java|active|GST tax-API integration for Indian compliance.
VendorIntegrationService|support|java|active|Third-party vendor API connectors.
PosDataSyncService|support|java|active|Tally ERP bidirectional sync orchestrator.
TallyConnector|support|java|active|Low-level Tally connector lib used by PosDataSyncService.
mongoEventListner|support|java|active|Standalone Mongo change-stream listener variant.
PosPythonBackend|support|python|active|OCR, bank-statement parsing, AI Assistant via Ollama. Flask on port 5100.
oneshell-commons|shared|java|active|Shared models (DAOs, v1 domain), reactive MongoDB utils, NATS JetStream client 2.20.2.
PosFrontend|frontend|node|active|Main POS UI — React 16.14 + Electron desktop/web app.
PosAdmin|frontend|node|active|Admin portal — React 18.3 + Vite. Dev server port 5174.
NodeInvoiceThemes|frontend|node|active|Invoice template renderer assets.
SetupRelated|infra|yaml|active|Cluster setup manifests — MongoDB Percona operator, ArgoCD apps, maintenance CronJobs.
gitops-repo|infra|yaml|active|ArgoCD GitOps source of truth for QA (qa-apps/) + prod (apps/) deployments.
PosDeployment|infra|yaml|maintenance|Older Docker deployment configs. Superseded by gitops-repo for K8s.
pos-deployment|infra|yaml|archive|Legacy lowercase duplicate of PosDeployment.
PosDockerPullService|infra|java|active|Pulls Docker images for on-prem Docker POS installs.
PosDockerSyncService|infra|java|active|Syncs Docker deployment state back to server.
ComposeUpdater|infra|unknown|archive|Docker-compose auto-updater utility. Last commit Jul 2025.
OneshellInstaller|infra|node|active|Customer-side installer bundle (CamelCase authoritative).
oneshell-installer|infra|node|archive|Lowercase duplicate — superseded by OneshellInstaller.
oneshell-utility-tool|infra|node|archive|Misc utility tool. Last commit Nov 2024.
keycloak-two-factor-auth-extension|infra|java|archive|Keycloak 2FA plugin. Last commit Dec 2024.
PosNodeBackend|support|node|maintenance|Older Node backend — mostly superseded by Java services.
PosNodePrinterUtil|support|node|archive|Thermal-printer helper. Last commit Nov 2024.
AIForgeCrew|ai|python|active|This repo — Pipeline v4 dispatchers, aiforge skills, Hermes + Hindsight integration, OneShell agent orchestration.
ClawdBot|ai|python|active|Telegram bot — OpenClaw gateway / agent messenger.
Azure-Ocr|poc|python|maintenance|Azure OCR POC. Last commit Nov 2025 — PosPythonBackend uses Ollama instead.
StockExperiment|poc|python|active|Stock forecasting / trading experiments — NOT OneShell POS.
StoreIntelligence|poc|java|active|Retail analytics service.
TelecomCopilot|external|unknown|active|Telecom project — unrelated to OneShell POS.
gac-gs3-diagnostic|external|python|active|GAC GS3 car diagnostic tooling — unrelated.
gac-openpilot|external|none|archive|Empty dir, no git.
godaddy-webhook|external|go|archive|GoDaddy DNS webhook. Last commit Nov 2024.
tc-v146|external|unknown|archive|TC v1.4.6 snapshot — no git, static files only.
EOF

# Trim trailing comma + close statement
sed -i '' -e '$ s/,$//' "$TMPSQL"
cat >> "$TMPSQL" <<'SQL'
;

COMMIT;

-- Verify
SELECT COUNT(*) AS inventory_facts FROM memory_units
  WHERE bank_id='aiforge' AND 'repo-inventory' = ANY(tags);
SQL

echo ">>> applying $TMPSQL"
copy_sql "$TMPSQL"

echo
echo "Verify with aiforge-search:"
echo "  QUERY=\"TallyConnector purpose\" bash ~/.hermes/skills/aiforge/search/SKILL.md"
