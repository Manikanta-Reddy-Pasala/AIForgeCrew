#!/usr/bin/env bash
# scripts/paperclip-bootstrap-agents.sh — idempotent agent bootstrap for Paperclip.
# Creates EM + Tester + Sr Developer + Sr Architect under the OneShell company.
#
# Uses Paperclip's built-in `hermes_local` adapter (no bridge HTTP server needed —
# Paperclip reads agent work items and forwards them to the hermes local runtime
# over stdio). Paperclip role enum is fixed: ceo|cto|cmo|cfo|engineer|designer|
# pm|qa|devops|researcher|general.
#
# DESIGN → Paperclip role mapping:
#   EM           → pm       (closest to "planning + decomposition")
#   Tester       → qa
#   Sr Developer → engineer
#   Sr Architect → cto      (architecture + review mandate)
#
# Routes used (inferred from paperclipai server/dist/routes/agents.js):
#   GET    /api/companies
#   POST   /api/companies
#   PATCH  /api/companies/:id
#   GET    /api/companies/:id/agents
#   POST   /api/companies/:id/agents
#   PATCH  /api/agents/:id
#   DELETE /api/agents/:id
set -euo pipefail

BASE="${PAPERCLIP_BASE:-http://localhost:3100}"
COMPANY_NAME="${COMPANY_NAME:-OneShell}"
COMPANY_DESC="${COMPANY_DESC:-Solving Business Problems with Software}"

command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

api() {  # api METHOD PATH [BODY]
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body"
  else
    curl -sS -X "$method" "$BASE$path"
  fi
}

echo ">>> find or create company '$COMPANY_NAME'"
COMPANIES=$(api GET /api/companies)
COMPANY_ID=$(echo "$COMPANIES" | jq -r --arg n "$COMPANY_NAME" '[.[] | select(.name==$n)][0].id // empty')
if [[ -z "$COMPANY_ID" ]]; then
  NEW=$(api POST /api/companies "$(jq -nc --arg n "$COMPANY_NAME" --arg d "$COMPANY_DESC" '{name:$n, description:$d}')")
  COMPANY_ID=$(echo "$NEW" | jq -r '.id')
  echo "   created company_id=$COMPANY_ID"
else
  echo "   found    company_id=$COMPANY_ID"
  api PATCH "/api/companies/$COMPANY_ID" "$(jq -nc --arg d "$COMPANY_DESC" '{description:$d}')" >/dev/null
fi

refresh_agents() { EXISTING=$(api GET "/api/companies/$COMPANY_ID/agents"); }
refresh_agents

lookup_id() { echo "$EXISTING" | jq -r --arg n "$1" '[.[] | select(.name==$n)][0].id // empty'; }

# Terminate any stray "probe" from earlier testing (DELETE /api/agents/:id).
PROBE_ID=$(lookup_id "probe")
if [[ -n "$PROBE_ID" ]]; then
  echo ">>> deleting stray probe agent $PROBE_ID"
  api DELETE "/api/agents/$PROBE_ID" >/dev/null || true
  refresh_agents
fi

# upsert_agent NAME ROLE TITLE REPORTS_TO EXTRA_JSON
upsert_agent() {
  local name="$1" role="$2" title="$3" reports_to="$4" extra="$5"
  local aid body
  aid=$(lookup_id "$name")
  body=$(jq -nc \
    --arg n "$name" --arg r "$role" --arg t "$title" --arg rt "$reports_to" \
    --argjson extra "$extra" \
    '{name:$n, role:$r, title:$t} + (if $rt=="" then {} else {reportsTo:$rt} end) + $extra')
  if [[ -z "$aid" ]]; then
    echo ">>> create agent: $name  (paperclip-role=$role, adapter=hermes_local)"
    api POST "/api/companies/$COMPANY_ID/agents" "$body" >/dev/null
  else
    echo ">>> update agent: $name  id=$aid"
    api PATCH "/api/agents/$aid" "$body" >/dev/null
  fi
}

# Agent defs per DESIGN §3. capabilities must be a single string per Paperclip schema.
upsert_agent "Engineering Manager" "pm" "Engineering Manager" "" "$(jq -nc '{
  adapterType:"hermes_local",
  adapterConfig:{},
  capabilities:"planning, task decomposition, acceptance criteria, test scenarios (cloud LLM)",
  budgetMonthlyCents:5000,
  permissions:{canCreateAgents:false}
}')"

refresh_agents
EM_ID=$(echo "$EXISTING" | jq -r '[.[] | select(.role=="pm" and .name=="Engineering Manager")][0].id // empty')
[[ -n "$EM_ID" ]] || { echo "EM lookup failed" >&2; exit 1; }

upsert_agent "Tester" "qa" "QA / Tester" "$EM_ID" "$(jq -nc '{
  adapterType:"hermes_local",
  adapterConfig:{},
  capabilities:"TDD tests, Playwright MCP + browser validation, coverage reporting",
  budgetMonthlyCents:0,
  permissions:{canCreateAgents:false}
}')"

upsert_agent "Sr Developer" "engineer" "Senior Developer" "$EM_ID" "$(jq -nc '{
  adapterType:"hermes_local",
  adapterConfig:{},
  capabilities:"code generation, refactoring, bug fixing, make failing tests pass",
  budgetMonthlyCents:0,
  permissions:{canCreateAgents:false}
}')"

upsert_agent "Sr Architect" "cto" "Senior Software Architect" "$EM_ID" "$(jq -nc '{
  adapterType:"hermes_local",
  adapterConfig:{},
  capabilities:"code review, security audit, architecture compliance, coverage gate, MR creation",
  budgetMonthlyCents:0,
  permissions:{canCreateAgents:false}
}')"

echo
echo "=== agents ==="
api GET "/api/companies/$COMPANY_ID/agents" \
  | jq -r '.[] | "  \(.name | .+ (" " * (24 - length)))  role=\(.role | .+ (" " * (10-length)))  adapter=\(.adapterType)  reportsTo=\(.reportsTo // "-")  status=\(.status)"'
echo
echo "Company UI: $BASE/company/$COMPANY_ID"
echo "UI tunnel:  ssh -L 3100:localhost:3100 manikanta@<mac-studio-ip>"
echo "Open:       http://localhost:3100"
