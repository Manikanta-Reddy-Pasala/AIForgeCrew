# Codebase Index — AIForgeCrew + OneShell (2026-04-20)

This map is injected into Sr Dev AGENTS.md so local models know the layout without reading every file.

## Repositories on Mac Studio `~/codeRepo/`

| Repo | Lang/Runtime | Port | Purpose |
|------|--------------|------|---------|
| AIForgeCrew | Python 3.12 / uv | — | This project. Agent runtime + Paperclip wrapper + MLX model setup |
| PosPythonBackend | Python 3.9 Flask | 5100 | OCR for bank statements, invoice OCR, AI Assistant (Ollama) |
| PosClientBackend | Spring Boot 3.3 / Java 21 / WebFlux | 8090 | Main client API, GraphQL. Docker + K8s |
| PosServerBackend | Spring Boot 3.3 / Java 21 / WebFlux | 8091 | Cloud backend, receives Docker sync, change streams |
| MongoDbService | Spring Boot 3.1 / Java 17 | 8080 | **MANDATORY** MongoDB gateway — never query Mongo direct |
| TallyConnector | Spring Boot / Java | — | Tally ↔ OneShell bidirectional sync |
| PosDataSyncService (PDS) | Spring Boot / Java | — | PosClient ↔ PosServer sync + verify-counts |

## PosPythonBackend layout

```
app/
  config.py              — Flask config
  routes/
    pythonBankOCR.py     — DISPATCHER. Registers each bank handler. New banks register here.
    invoice_ocr.py       — Invoice OCR (413 lines)
    data.py              — data CRUD
    HetznerFileRoutes.py — file upload
  util/
    boi_bank_handler.py     — Bank of India statement parser
    icici_handler.py        — ICICI
    indian_bank_handler.py  — Indian Bank
    karnataka_bank_handler.py — Karnataka Bank
    kotak_bank_handler.py   — Kotak
    sbi_bank_handler.py     — SBI
    yes_bank_handler.py     — Yes Bank
    helpers.py              — shared utilities
  services/                 — business logic
  ml/                       — ML endpoints (Store Intelligence)
  assistant/                — AI Assistant (Ollama)
tests/
  fixtures/                 — sample PDFs + golden JSON
  util/                     — per-handler tests
```

### Bank handler convention

Every handler exports a top-level function `<bank>_handler(pdf_path: str) -> dict`.

Return shape (legacy, v1):
```json
{
  "accountName": str,
  "accountNumber": str,
  "transactions": [ {"transactionId","date","description","chequeNo","crDr","transactionAmount","balance"}, ... ]
}
```

Return shape (v2 for new work, e.g. ONE-48):
```json
{
  "metadata": {"bank_name","branch","account_name","account_number","customer_id","account_type","ifsc_code","statement_period_start","statement_period_end","statement_generated_at"},
  "transactions": [ {"serial","txn_date (ISO)","description","chequeNo (nullable)","transactionAmount","transactionDirection (CR|DR)","balance"}, ... ]
}
```

Both `parse(pdf_path)` and `<bank>_handler = parse` alias must be exported.

### Bank handler registration

Add new bank to `app/routes/pythonBankOCR.py`:
```python
from app.util.<newbank>_handler import <newbank>_handler
# then extend the dispatch dict
```

### Indian number format

Bank statements use lakh/crore with mixed commas: `1,14,000.00` → `114000.00`, `2,30,51,566.93` → `23051566.93`. Use a parser that strips all commas then `float(...)` — do NOT rely on `,` as thousands separator.

### pdfplumber quirks

- `page.extract_tables()` returns list of lists; header row detection varies
- Long description cells wrap to multiple lines within same row — use `clean_text` to collapse
- Empty cells are `None`, not `""`
- Date columns come back as strings; parse with `re.match(r"(\d{2})[-/](\d{2})[-/](\d{4})", ...)`

### pytest conventions

- venv/bin/python is broken on Mac Studio; use `/usr/bin/python3`
- `tests/util/conftest.py` does `sys.path.insert(0, <repo root>)` so `from app.util.*` imports resolve
- Fixtures live in `tests/fixtures/`; each bank has `<bank>.pdf` and optional `<bank>_expected.json` golden
- Test file convention: `tests/util/test_<bank>_bank_handler.py`

## AIForgeCrew layout

```
agents/
  em/                    — Engineering Manager (claude-opus, role=pm)
  sr-architect/          — Sr Arch (gemma-4-26b, role=cto)
  sr-developer/          — Sr Dev (Reasoning) agent prompt + permissions
  tester/                — Tester (qwen3.5-9b, role=qa)
docs/
  architecture.md        — system design
  runbook.md             — ops
  troubleshooting.md     — known issues
  model-evaluation.md    — benchmarks
  eval/                  — bench CSVs
  tickets/               — ticket templates + fixtures (ONE-48 lives here)
  agents/                — agent-role split docs
  superpowers/plans/     — plan files
scripts/
  benchmark-*.sh         — Sr Dev / pass@k benchmarks
  boi-v2-*.sh            — BOI v2 re-eval dispatchers (direct / gemma-only)
  route-ticket.sh        — reasoning vs code routing helper
  delete-unused-models.sh
  compute-checksums.sh
security/
  model-checksums.yml    — pinned sha256 per MLX model
  network-allowlist.yml  — inference endpoint allowlist
aiforge_core/            — Python package (renamed from paperclip)
  skills/
```

## Paperclip company

- company_id: `fd294bd0-2f65-405f-b443-fb41d66226fb`
- API base: `http://127.0.0.1:3100/api`
- DB: embedded postgres via pg0 at `127.0.0.1:54329`, user=paperclip, pw=paperclip

### Agents

| Name | ID | Role | Model |
|------|----|------|-------|
| Engineering Manager | `35760e2f-4cef-4013-9aff-d93592b5f71e` | pm | claude-opus-4-7 |
| Sr Dev (Reasoning) | `28b8c064-bfcf-44e1-9e91-e37c39e0097c` | engineer | gemma-4-31b-it |
| Sr Dev (Coder) | `e0502e94-0608-4fb9-9afa-b70d8dbf014a` | engineer | qwen3-coder-next |
| Sr Architect | `0e173374-287c-4595-bf46-6ba26c11035f` | cto | gemma-4-26b-a4b-it |
| Tester | `eb1c388d-8601-4df4-89d8-447ec2ff5946` | qa | qwen3.5-9b-mlx |

### Labels

- `reasoning` (`db58c603-5c1d-47f8-ae3b-59bb13486216`) → Sr Dev (Reasoning)
- `code` (`3d471283-6dd3-408a-9ae4-61465833d33b`) → Sr Dev (Coder)

## Workflow anchors

Always:
1. `hindsight_recall` the ticket topic FIRST (before any file read)
2. Check for labels `reasoning` vs `code` — only pick up tickets matching your role
3. Use `/usr/bin/python3` not `./venv/bin/python3` on Mac Studio
4. Commit immediately when tests pass; do NOT keep iterating after green
5. Golden fixture deep-equal is the definition of done for parsers

Key files you'll revisit:
- `~/codeRepo/PosPythonBackend/app/routes/pythonBankOCR.py` — registration
- `~/codeRepo/PosPythonBackend/tests/util/conftest.py` — sys.path shim
- `~/codeRepo/PosPythonBackend/tests/fixtures/` — PDFs + goldens

## MongoDB (cloud ops)

If a ticket requires MongoDB work: route via `MongoDbService` (port 8080). Never query Mongo direct from any service. Schema access for debugging:

```bash
kubectl exec -n mongodb prod-cluster-mongos-0 --insecure-skip-tls-verify -- mongosh \
  'mongodb://databaseAdmin:akyFqNelEclMhlkNx06c@localhost:27017/oneshell?authSource=admin' \
  --quiet --eval 'db.stats()'
```

## NATS subjects (sync flow)

- `business.push.request` — client → server push sync
- `pendingToSync` — client local retry queue
- `changestream.events.{collection}` — server change stream events
- `changestream.dlq.{collection}` — dead letter queue
