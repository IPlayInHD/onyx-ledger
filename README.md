# Onyx Ledger

**An AI-assisted Canadian tax audit platform.** Onyx Ledger reads a taxpayer's
documents, computes their full federal + provincial position, scores their **Tax
Health**, and shows them — in plain language, with statute citations — how to
legally pay less. *Powered by ONYX Intelligence.*

> Educational estimates only. Onyx Ledger does not file returns and is not
> affiliated with or endorsed by the Canada Revenue Agency.

---

## What's in this repo

```
onyx-ledger/
├── netlify.toml            Netlify deploy config (serverless API + static frontend)
├── NETLIFY.md              Step-by-step deployment guide
├── static-site/            Zero-backend build — the whole app runs in the browser
│                           (drag-and-drop upload to any static host)
└── server/                 The Node app: tax engine + API + frontend
    ├── engine/             The tax engine (pure, tested)
    │   ├── taxData.js       2024 + 2025 federal & provincial constants (data)
    │   ├── taxEngine.js     T1-style computation (tax, credits, brackets, cash-flow)
    │   ├── extract.js       Document scanner (T4/T5/T2202/… + OCR-text)
    │   ├── scoring.js       0–100 Tax Health score
    │   ├── advisory.js      Opportunity rules engine (cited strategies)
    │   ├── checklist.js     Personalized "documents to add"
    │   ├── planner.js       RRSP optimizer, benefit estimates, tax calendar
    │   └── index.js         runAudit() + simulate() orchestrators
    ├── public/             Frontend: landing, auth, dashboard, document guide
    ├── scripts/            Reproducible static-site build
    ├── test/               Engine test suite (139 assertions)
    ├── server.js           Express API (accounts, docs, audit, simulate, export)
    └── store.js            Persistence (JSON file locally / Netlify Blobs in prod)
```

## Two ways to run it

**1. Static (no backend)** — the engine is pure JavaScript, so `static-site/`
runs the entire product in the browser, with accounts and audits saved in
`localStorage`. Upload those files to any static host, or open `index.html`
locally. Rebuild with:

```bash
cd server && npm run build:static
```

**2. Full server** — real accounts (JWT + bcrypt), an Express API, and
persistence (Netlify Blobs in production, a JSON file locally).

```bash
cd server
npm install
npm test        # 139 engine assertions
npm start       # http://localhost:4000
```

Deploy the full version to Netlify — see **[NETLIFY.md](NETLIFY.md)**.

## Features

- **Tax engine** — 2024 & 2025 tax years, federal + all 13 provinces/territories
  (brackets, credits, CPP/EI, dividends, capital gains, Ontario surtax + health
  premium, Quebec abatement).
- **Document scanner** — reads structured entries or OCR/PDF text for T4, T4A,
  T5, T3, T2202, RRSP, FHSA, donations, medical, T5008, T4E, child-care, with a
  pluggable OCR provider interface.
- **Tax Health score** and a personalized **document checklist**.
- **Advisory engine** — legal tax-reduction opportunities, each with an estimated
  dollar impact and a statute citation.
- **Live What-if planner** — sliders that recompute refund/marginal/bracket in
  real time. **RRSP optimizer** that solves the contribution to erase owing or
  drop a bracket. **Estimated benefits** (GST/HST, Canada Carbon Rebate, CCB).
  **Tax calendar** with live deadline countdowns.
- **Trust & control** — "how this was calculated" transparency, encryption-forward
  Security Centre, a full CRA document guide, and data controls (export / delete).

## Accuracy & scope

Tax constants live in `engine/taxData.js`, separate from the calculation logic,
and are legislated **annually** — verify them against CRA and provincial sources.
2025 **federal** figures are final; 2025 **provincial** figures are indexed
estimates pending verification. The engine models the mainstream T1 calculation
and documents its simplifications in-source. It is an **auditor-grade estimate**,
not a filed return. Image OCR requires wiring an OCR provider into
`engine/extract.js`.

## Testing

`npm test` runs 139 assertions: hand-computed reference cases, the Quebec
abatement, RRSP marginal savings, the full document + audit pipeline, 2025 tax
year, simulate/optimizer/benefits/calendar, and property tests (monotonicity,
marginal ≥ average, no negative tax) across provinces and income levels. The
frontend is verified end-to-end in a headless browser.
