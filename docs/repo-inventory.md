# OneShell codeRepo inventory

**Last audited**: 2026-04-20 (ONE-54)
**Source**: `~/Documents/codeRepo/` (laptop) · `~/codeRepo/` (Mac Studio mirror)
**Agents**: loaded into Hindsight `aiforge` bank via `scripts/seed-repo-inventory.sh`

Categories:
- `core` — primary OneShell POS services
- `support` — auxiliary services (comms, notifications)
- `shared` — libraries reused across services
- `infra` — deployment, setup, ops
- `frontend` — user-facing apps
- `poc` — experimental / non-production
- `archive` — maintenance-only or deprecated (no recent commits)
- `external` — unrelated side projects (skip for OneShell tickets)

Status: `active` (< 60d last commit) · `maintenance` (60-180d) · `archived` (> 180d or dead).

## Core POS services (Java Spring Boot)

| Repo | Lang | Status | Purpose |
|---|---|---|---|
| `MongoDbService` | java (3.1/17) | active | **Mandatory MongoDB gateway.** All services query via this. Port 8080. |
| `PosClientBackend` | java (3.3/21) | active | Main client API + GraphQL. Docker + K8s `pos` ns. Port 8090. Push-syncs to PosServerBackend via NATS. |
| `PosServerBackend` | java (3.3/21) | active | Cloud backend. Receives Docker sync, runs change streams, applies sync rules. Port 8091. |
| `GatewayService` | java (2.7/17) | active | API Gateway + JWT auth. Port 8080. |
| `BusinessService` | java (2.7/21) | active | Business logic for PosAdmin + mobile. Port 8080. |
| `PosService` | java (21) | active | POS ops. Port 8081. |
| `Scheduler` | java (3.2/21) | active | Recurring events, invoices. Port 8080. |
| `QuartzScheduler` | java (3.2/21) | active | Stock/balance corrections, batch jobs. Port 8080. |

## Support services

| Repo | Lang | Status | Purpose |
|---|---|---|---|
| `EmailService` | java | active | Email send/template service. |
| `NotificationService` | java | active | Push/in-app notifications. |
| `WhatsappApiService` | java | maintenance | WhatsApp messaging (Feb 2026 last touch). |
| `GstApiService` | java | active | GST tax-API integration. |
| `VendorIntegrationService` | java | active | Third-party vendor APIs. |
| `PosDataSyncService` | java | active | Tally bidirectional sync. |
| `TallyConnector` | java | active | Tally ERP connector used by PosDataSyncService. |
| `mongoEventListner` | java | active | Mongo change-stream listener (standalone variant). |
| `PosPythonBackend` | python (Flask) | active | OCR, bank-statement parsing, AI Assistant (Ollama). Port 5100. |

## Shared libraries

| Repo | Lang | Status | Purpose |
|---|---|---|---|
| `oneshell-commons` | java | active | Shared models (DAOs, v1 domain), reactive MongoDB utils, NATS client. |

## Frontend

| Repo | Lang | Status | Purpose |
|---|---|---|---|
| `PosFrontend` | node (React 16.14 + Electron) | active | Main POS UI, desktop + web. |
| `PosAdmin` | node (React 18.3 + Vite) | active | Admin portal, port 5174 in dev. |
| `NodeInvoiceThemes` | node | active | Invoice template renderer. |

## Infrastructure / deployment / ops

| Repo | Lang | Status | Purpose |
|---|---|---|---|
| `SetupRelated` | yaml/k8s | active | Cluster setup manifests — MongoDB Percona, ArgoCD apps, CronJobs. |
| `gitops-repo` | yaml | active | ArgoCD GitOps source-of-truth for QA + prod deployments. |
| `PosDeployment` | yaml/shell | maintenance | Older Docker deployment configs. Superseded by `gitops-repo` for K8s. |
| `pos-deployment` | yaml/shell | archive | Legacy duplicate of `PosDeployment`. |
| `PosDockerPullService` | java | active | Pulls Docker images for on-prem Docker POS installs. |
| `PosDockerSyncService` | java | active | Syncs Docker deployments state to server. |
| `ComposeUpdater` | ? | archive | Docker-compose updater utility (Jul 2025 last touch). |
| `OneshellInstaller` | node | active | Customer-side installer bundle. |
| `oneshell-installer` | node | archive | Lowercase duplicate — superseded by `OneshellInstaller`. |
| `oneshell-utility-tool` | node | archive | Misc utility (Nov 2024 last touch). |
| `keycloak-two-factor-auth-extension` | java | archive | Keycloak 2FA plugin (Dec 2024 last touch). |

## Node backend (maintenance)

| Repo | Lang | Status | Purpose |
|---|---|---|---|
| `PosNodeBackend` | node | maintenance | Older Node backend; superseded by Java services. |
| `PosNodePrinterUtil` | node | archive | Thermal-printer helper (Nov 2024 last touch). |

## AI + agents

| Repo | Lang | Status | Purpose |
|---|---|---|---|
| `AIForgeCrew` | python | active | **This repo.** Pipeline v4 dispatchers, skills, Hermes/Hindsight integration. |
| `ClawdBot` | python | active | Telegram bot (OpenClaw gateway / agent messenger). |

## POCs / experiments

| Repo | Lang | Status | Purpose |
|---|---|---|---|
| `Azure-Ocr` | python | maintenance | Azure OCR POC (Nov 2025 last touch); PosPythonBackend uses Ollama instead. |
| `StockExperiment` | python | active | Stock forecasting / trading experiments — not OneShell POS. |
| `StoreIntelligence` | java | active | Retail analytics service. |

## External (not OneShell POS — skip for OneShell tickets)

| Repo | Lang | Status | Purpose |
|---|---|---|---|
| `TelecomCopilot` | ? | active | Telecom project. |
| `gac-gs3-diagnostic` | python | active | GAC GS3 car diagnostic tooling. |
| `gac-openpilot` | — | archive | Empty dir, no git. |
| `godaddy-webhook` | go | archive | GoDaddy DNS webhook (Nov 2024 last touch). |
| `tc-v146` | — | archive | TC v1.4.6 snapshot (no git). |

## How agents should use this

Before touching a repo, `aiforge-search` with the repo name — hindsight returns the row above plus any prior-ticket experiences. For ticket impact analysis, start with `core` + `shared`, then cascade to `support` + `frontend` as needed. Never modify `archive` repos without human confirmation.

Re-seed after edits: `bash scripts/seed-repo-inventory.sh`.
