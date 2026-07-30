# ONYX Intelligence — Canadian Tax Audit Engine & API

The backend that powers **Onyx Ledger**: it scans a user's tax documents, computes
their full federal + provincial tax position, scores their **Tax Health**, and
generates an **advisory audit** of legal, cited opportunities to pay less tax.

> **It does not file taxes.** It is an educational engine that estimates a filer's
> position and explains — in plain language, with statute references — what they
> may be eligible for. Every figure is an estimate, not tax advice.

---

## What it does

1. **Scans documents** (`engine/extract.js`) — reads T4, T4A, T5, T3, T2202, RRSP,
   FHSA, donation, medical, T5008, T4E and child-care slips. Accepts either
   structured box values or raw OCR/PDF **text** (regex box-scanning), with a
   per-document **confidence** score. Real image OCR is behind a pluggable
   `ocrProvider` interface.
2. **Computes the return** (`engine/taxEngine.js`) — brackets, the major
   non-refundable credits, dividend gross-up/DTC, capital-gains inclusion,
   Ontario surtax + health premium, and the Quebec abatement, for **all 13
   provinces/territories**. Returns income, taxable income, federal & provincial
   tax, refund/balance, and marginal & average rates.
3. **Scores Tax Health** (`engine/scoring.js`) — a 0–100 score across five
   weighted categories (registered savings, deduction/credit capture, tax
   efficiency, documentation, planning).
4. **Finds opportunities** (`engine/advisory.js`) — a rules engine encoding the
   legal tax-reduction strategies a Canadian tax accountant/auditor looks for
   (RRSP/FHSA/TFSA, tuition transfer, medical pooling, donation optimization,
   pension splitting, CWB/CCB, child care, employment expenses, capital-loss
   harvesting, instalments…). Each rule estimates a **dollar impact** at the
   filer's marginal rate and cites the relevant provision.
5. **Composes an advisory summary** — headline, prioritized actions, total
   estimated opportunity, and a clear disclaimer.

## Accuracy & the tax constants

`engine/taxData.js` holds the **2024** federal and provincial parameters as data,
separate from the calculation logic. These are legislated/indexed **annually** and
must be validated against official CRA and provincial finance publications before
production use. Adding a new tax year is **data-only** — no logic changes.

The engine intentionally simplifies parts of the T1 (uses net income as a proxy
for taxable income; approximates provincial dividend credits and some
province-specific amounts). It is an **auditor-grade estimate**, documented in the
source.

Verified by `test/engine.test.js` (38 assertions), including a hand-computed
Ontario \$60,000 case (federal \$6,002.95 + provincial \$3,134.50), the Quebec
abatement, RRSP marginal savings, the full document-extraction pipeline, and all
13 jurisdictions.

## API

| Method | Path | Auth | Purpose |
|--------|------|:----:|---------|
| POST | `/api/auth/register` | – | Create account → `{ token, user }` |
| POST | `/api/auth/login` | – | Sign in → `{ token, user }` |
| GET | `/api/me` | ✓ | Current user |
| GET/PUT | `/api/profile` | ✓ | Demographic + planning profile |
| GET/POST | `/api/documents` | ✓ | List / add a document (scanned on add) |
| POST | `/api/documents/upload` | ✓ | Multipart file upload (text slips) |
| DELETE | `/api/documents/:id` | ✓ | Remove a document |
| POST | `/api/audit` | ✓ | Run the full audit and store it |
| GET | `/api/audit` | ✓ | Latest stored audit (`{audit:null}` if none) |
| GET | `/api/meta` | – | Provinces + supported slip types |

Auth is JWT (`bcryptjs` password hashing). Persistence is a zero-dependency JSON
store (`store.js`) — swap for Postgres in production; the server only uses its
methods.

## Frontend (`public/`)

- `index.html` — product landing page
- `signup.html` / `login.html` — account creation & sign-in
- `dashboard.html` — the audit dashboard: profile editor, document scanner UI,
  and the rendered audit (tax position, Tax Health, opportunities, advisory)
- `onyx.css` / `app.js` — shared design system + API client

## Run it

```bash
cd server
npm install
npm test        # engine assertions
npm start       # http://localhost:4000
```

Then open `http://localhost:4000`, create an account, add the sample slips, and
run your audit.

## Disclaimer

Educational estimates generated from user-provided information. Not tax advice,
not an audit-risk assessment, not a filed return, and not affiliated with the CRA.
Confirm specifics with the CRA or a licensed tax professional before acting.
