# Chemical Invoice Ingestion

Sentinel ingests pool-supply invoice PDFs, extracts line items, normalizes product names, validates math, and stores everything in SQLite for cost reporting.

---

## How it works

```
PDF in inbox/
  → extract text (pdfplumber)
  → parse header + line items
  → normalize each line (dictionary → alias DB → AI → fallback)
  → reconcile: sum(extended_price) == subtotal, subtotal + tax == total
  → write to DB (RECONCILED or REVIEW_NEEDED)
  → move PDF to processed/ or failed/ with a safe deterministic filename
```

The reconciliation is always deterministic math. AI only acts as a data-entry clerk during extraction and product classification — it never decides whether dollars are trusted.

---

## Extraction modes

| `CHEM_INVOICE_EXTRACTOR_MODE` | What it does |
|---|---|
| `stub` | Returns a hard-coded Heritage Pool Supply fixture. No PDF parsing. Use for smoke tests. |
| `text` | pdfplumber text extraction + Heritage Pool Supply regex parser. Works offline. |
| `openai` | pdfplumber text extraction + GPT call (structured JSON prompt). Most accurate for new vendors. Requires `OPENAI_API_KEY`. |

Default: `text`

---

## Env vars

| Variable | Default | Purpose |
|---|---|---|
| `CHEM_INVOICE_INBOX_DIR` | `/data/chemical_invoices/inbox` | Drop PDFs here for pickup |
| `CHEM_INVOICE_PROCESSED_DIR` | `/data/chemical_invoices/processed` | Successfully ingested PDFs moved here |
| `CHEM_INVOICE_FAILED_DIR` | `/data/chemical_invoices/failed` | PDFs that errored during ingestion |
| `CHEM_INVOICE_MAX_PDF_BYTES` | `26214400` | Reject local inbox PDFs larger than 25 MB and move them to `failed/` |
| `CHEM_INVOICE_AI_CLASSIFICATION_ENABLED` | `1` | When `openai` extraction is active, allows AI fallback classification for unknown line items |
| `CHEM_INVOICE_EXTRACTOR_MODE` | `text` | `stub` / `text` / `openai` |
| `CHEM_INVOICE_RECONCILE_TOLERANCE` | `0.02` | Max dollar difference allowed before REVIEW_NEEDED |
| `CHEM_INVOICE_OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model (only used in `openai` mode) |

---

## Triggering ingest

Drop PDF files into `CHEM_INVOICE_INBOX_DIR` (default `/data/chemical_invoices/inbox`) and call:

```bash
./curl_job.sh /jobs/chemical_invoices/ingest_local
```

The job scans for `*.pdf` files, processes each one, and moves them to `processed/` or `failed/`. Returns a JSON summary of counts per status.

Moved files are renamed safely to:

```text
{vendor_slug}_{invoice_number}_{sha8}.pdf
```

Examples:

```text
heritage_pool_supply_inv_12345_ab12cd34.pdf
unknown_vendor_unknown_invoice_ab12cd34.pdf
```

If a target filename already exists in `processed/` or `failed/`, Sentinel appends `_2`, `_3`, etc. rather than overwriting the older file.

**Future Gmail poller:** The inbox directory design means a future Google Apps Script or Gmail monitor can write PDFs directly to this directory and the same job picks them up without any code changes.

---

## Invoice statuses

| Status | Meaning |
|---|---|
| `RECONCILED` | Math checks out — dollars trusted |
| `REVIEW_NEEDED` | Math mismatch or extraction failure — needs human review |
| `REVIEWED` | Admin has manually reviewed and accepted |
| `DUPLICATE` | Already in DB (blocked by PDF SHA-256 or vendor+invoice_number) |

The local inbox scan can also return non-invoice ingest outcomes:

| Scan Result | Meaning |
|---|---|
| `EXTRACTION_FAILED` | PDF could not be parsed/extracted; no invoice row was created |
| `PDF_TOO_LARGE` | PDF exceeded `CHEM_INVOICE_MAX_PDF_BYTES`; file moved to `failed/` |

---

## Product normalization

### Priority order

1. **APPROVED alias** — admin-confirmed mapping in `chemical_product_aliases`. Always wins.
2. **Normalization dictionary** — seeded rules in `services/chemical_invoices.py`. Matched on substring in the raw description. Recorded as APPROVED in the alias table automatically.
3. **AI suggestion** (only in `openai` mode) — if confidence ≥ 0.90, recorded as `AI_SUGGESTED`. If < 0.90, recorded as `NEEDS_REVIEW` and classified as `category=other, cost_type=misc`.
4. **Fallback** — `normalized_product_name=unknown, category=other, cost_type=misc, confidence=0.0`.

### Current dictionary (seeded for Heritage Pool Supply)

| Matches (any substring) | Normalized Name | Category | Cost Type |
|---|---|---|---|
| POOL BREEZE GRANULAR, CAL HYPO, CALCIUM HYPOCHLORITE | calcium hypochlorite | shock | chemical |
| CHLORINATING TABLETS, TRICHLOR, TABS | chlorine tablets | tabs | chemical |
| MURIATIC ACID | muriatic acid | acid | chemical |
| SODIUM BICARBONATE, BICARBONATE | sodium bicarbonate | alkalinity | chemical |
| AQUASALT, POOL SALT, SALT BAG | pool salt | salt | chemical |
| PHOSFIGHT, PHOSPHATE REMOVER | phosphate remover | phosphate_remover | chemical |
| STRIKE-OUT ALGAECIDE, ALGAECIDE | algaecide | algaecide | chemical |
| DIATOMACEOUS EARTH | diatomaceous earth | filter_media | chemical |
| TAYLOR, THIOSULF, SULF ACID, CYANURIC ACID REAGENT, REAGENT | testing reagent | reagent/testing | testing |
| PENTAIR BLENDED, NYLON BRUSH, BRUSH | pool brush | equipment_part | equipment |

The dictionary is a seed — it doesn't need to be exhaustive. Unknown products flow into the alias learning loop.

---

## Product alias learning loop

Unknown products (no dictionary match) create a `chemical_product_aliases` row. In `openai` extraction mode, Sentinel can optionally ask AI for a suggested classification **only** when all of these are true:

- `CHEM_INVOICE_EXTRACTOR_MODE=openai`
- `AI_GATE_ENABLED=1`
- `CHEM_INVOICE_AI_CLASSIFICATION_ENABLED=1`

If the AI confidence is `>= 0.90`, the alias is stored as `AI_SUGGESTED`. Otherwise it stays `NEEDS_REVIEW`.

Sentinel does **not** auto-approve these mappings and does **not** overwrite `APPROVED` aliases.

Once reviewed:

```bash
# See what needs review
curl -s "https://sentinel.northtexaspoolpros.com/chemical_products/aliases?status=NEEDS_REVIEW" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET" | jq .

# Approve with corrections
curl -X POST "https://sentinel.northtexaspoolpros.com/chemical_products/aliases/42/approve" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"approved_by": "kevin", "normalized_product_name": "clarifier", "category": "clarifier", "cost_type": "chemical"}'

# Or just update fields without approving yet
curl -X POST "https://sentinel.northtexaspoolpros.com/chemical_products/aliases/42/update" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"category": "chemical", "cost_type": "chemical"}'
```

Once APPROVED, future invoices with the same `vendor_name + item_code` automatically use the approved mapping — no re-review needed.

---

## API endpoints

All endpoints require `X-NTPP-Secret` header.

### Ingest

```
POST /jobs/chemical_invoices/ingest_local
```

Scans inbox, processes all PDFs. Returns:
```json
{
  "job": "chemical_invoices/ingest_local",
  "scanned": 4,
  "counts": {"RECONCILED": 3, "REVIEW_NEEDED": 1, "DUPLICATE": 0},
  "results": [...]
}
```

### Read invoices

```
GET /chemical_invoices?status=REVIEW_NEEDED&vendor=Heritage&limit=50&offset=0
GET /chemical_invoices/{id}
GET /chemical_invoices/summary?start_date=2026-04-01&end_date=2026-04-30
```

Summary response shape:
```json
{
  "period": {"start_date": "2026-04-01", "end_date": "2026-04-30"},
  "total_spend": 1423.29,
  "chemical_spend": 1223.00,
  "invoice_count": 4,
  "reconciled_count": 4,
  "review_needed_count": 0,
  "by_vendor": {"Heritage Pool Supply": {"spend": 1423.29, "invoices": 4}},
  "by_category": {"shock": {"spend": 615.00, "line_count": 3}, ...},
  "by_product": {"calcium hypochlorite": {"spend": 615.00, "quantity": 3.0, "default_unit": "lb"}, ...},
  "by_cost_type": {"chemical": {"spend": 1223.00}, "equipment": {"spend": 38.90}, ...}
}
```

### Review

```
POST /chemical_invoices/{id}/mark_reviewed
Body: {"reviewed_by": "kevin"}
```

### Alias management

```
GET  /chemical_products/aliases?status=NEEDS_REVIEW&limit=50
POST /chemical_products/aliases/{id}/approve
     Body: {"approved_by": "kevin", "normalized_product_name": "...", "category": "...", "cost_type": "..."}
POST /chemical_products/aliases/{id}/update
     Body: {"category": "chemical", "cost_type": "chemical"}
```

---

## Database tables

| Table | Purpose |
|---|---|
| `chemical_invoices` | One row per invoice. `pdf_sha256` + `(vendor_name, invoice_number)` are both unique constraints. |
| `chemical_invoice_lines` | Line items. `invoice_id` FK to `chemical_invoices`. |
| `chemical_product_aliases` | Product mapping registry. `mapping_status`: APPROVED / AI_SUGGESTED / NEEDS_REVIEW / REJECTED. |
| `chemical_invoice_extraction_runs` | Audit log of every extraction attempt (success or failure). |

---

## Running tests

```bash
# Inside the container
docker exec -it ntpp-sentinel sh -c "pip install pytest && python -m pytest app/tests/test_chemical_invoices.py -v"

# Or locally (if Python + pytest available)
cd app && pip install pytest && python -m pytest tests/test_chemical_invoices.py -v
```

Tests use an in-memory SQLite database. No OpenAI key, no pdfplumber, no network required.

---

## Reconciliation rules (invariants)

1. `sum(line_item.extended_price)` must equal `subtotal` within `CHEM_INVOICE_RECONCILE_TOLERANCE` (default $0.02).
2. `subtotal + tax + fees` must equal `invoice_total` within the same tolerance.
3. Both conditions must hold for `RECONCILED`. Either failing → `REVIEW_NEEDED`.
4. A `REVIEW_NEEDED` invoice is stored with all its data intact but flagged — no data is discarded.
5. If `invoice_number` is missing, Sentinel relies on PDF SHA-256 for duplicate detection and forces `REVIEW_NEEDED` even if the dollar math matches.
6. Extraction failures and oversized local PDFs do **not** create partial invoice rows; they are logged in `chemical_invoice_extraction_runs` and moved to `failed/`.
5. Duplicate PDFs (same SHA-256) or duplicate vendor+invoice_number are rejected as `DUPLICATE` before any processing.

---

## Known limitations

- **`text` mode is Heritage Pool Supply-specific.** The regex parser is tuned for their invoice layout. Other vendors should use `openai` mode.
- **pdfplumber text extraction quality varies** by PDF generator. If extraction produces garbled text, switch to `openai` mode or drop the PDF manually after reviewing it.
- **No cron schedule yet.** The ingest job must be triggered manually (`./curl_job.sh`) or via the Gmail poller script. Add a `CRON_CHEM_INVOICE_*` schedule to `cron/render-crontab.sh` when ready.
