/**
 * ONYX Intelligence — document scanner / extraction layer
 * =========================================================================
 * Turns uploaded tax documents into normalized financial fields the engine
 * understands. Two input paths:
 *   1) STRUCTURED  { type, fields:{...} }  — highest confidence (0.99).
 *   2) TEXT        { type, text:"..." }    — OCR / PDF text; scanned with
 *      slip-aware regex heuristics and a confidence derived from how many
 *      expected boxes were located.
 *
 * Real image OCR (photo/scan -> text) is intentionally behind a pluggable
 * `ocrProvider` interface — swap in Tesseract or a cloud OCR in production.
 * The engine and advisory never depend on how the text was produced.
 * =========================================================================
 */

'use strict';

// Which normalized fields each slip type can contribute, and the box/keyword
// hints used to scan free text. Values are summed across all documents.
const SLIP_MAP = {
  T4: {
    label: 'T4 — Statement of Remuneration Paid',
    fields: {
      employmentIncome: [/box\s*14[^0-9]{0,12}([\d,]+\.?\d*)/i, /employment income[^0-9]{0,12}([\d,]+\.?\d*)/i],
      cppContrib: [/box\s*16[^0-9]{0,12}([\d,]+\.?\d*)/i, /cpp contributions[^0-9]{0,12}([\d,]+\.?\d*)/i],
      eiContrib: [/box\s*18[^0-9]{0,12}([\d,]+\.?\d*)/i, /ei premiums[^0-9]{0,12}([\d,]+\.?\d*)/i],
      taxWithheld: [/box\s*22[^0-9]{0,12}([\d,]+\.?\d*)/i, /income tax deducted[^0-9]{0,12}([\d,]+\.?\d*)/i],
      unionDues: [/box\s*44[^0-9]{0,12}([\d,]+\.?\d*)/i, /union dues[^0-9]{0,12}([\d,]+\.?\d*)/i],
      donations: [/box\s*46[^0-9]{0,12}([\d,]+\.?\d*)/i],
    },
    expected: ['employmentIncome', 'taxWithheld'],
  },
  T4A: {
    label: 'T4A — Pension, Retirement, Annuity, Other',
    fields: {
      pensionIncome: [/box\s*016[^0-9]{0,12}([\d,]+\.?\d*)/i, /pension[^0-9]{0,12}([\d,]+\.?\d*)/i],
      selfEmploymentIncome: [/box\s*048[^0-9]{0,12}([\d,]+\.?\d*)/i, /fees for services[^0-9]{0,12}([\d,]+\.?\d*)/i],
      taxWithheld: [/box\s*022[^0-9]{0,12}([\d,]+\.?\d*)/i],
    },
    expected: ['pensionIncome'],
  },
  T5: {
    label: 'T5 — Statement of Investment Income',
    fields: {
      eligibleDividends: [/box\s*24[^0-9]{0,12}([\d,]+\.?\d*)/i, /actual amount of eligible dividends[^0-9]{0,12}([\d,]+\.?\d*)/i, /box\s*10[^0-9]{0,12}([\d,]+\.?\d*)/i],
      nonEligibleDividends: [/box\s*10[^0-9]{0,12}([\d,]+\.?\d*)/i],
      interestIncome: [/box\s*13[^0-9]{0,12}([\d,]+\.?\d*)/i, /interest[^0-9]{0,12}([\d,]+\.?\d*)/i],
    },
    expected: ['interestIncome'],
  },
  T3: {
    label: 'T3 — Statement of Trust Income',
    fields: {
      eligibleDividends: [/box\s*49[^0-9]{0,12}([\d,]+\.?\d*)/i],
      capitalGains: [/box\s*21[^0-9]{0,12}([\d,]+\.?\d*)/i, /capital gains[^0-9]{0,12}([\d,]+\.?\d*)/i],
      otherIncome: [/box\s*26[^0-9]{0,12}([\d,]+\.?\d*)/i],
    },
    expected: [],
  },
  T2202: {
    label: 'T2202 — Tuition and Enrolment Certificate',
    fields: { tuition: [/box\s*23[^0-9]{0,12}([\d,]+\.?\d*)/i, /eligible tuition[^0-9]{0,12}([\d,]+\.?\d*)/i, /tuition[^0-9]{0,12}([\d,]+\.?\d*)/i] },
    expected: ['tuition'],
  },
  RRSP: {
    label: 'RRSP Contribution Receipt',
    fields: { rrspDeduction: [/contribution[^0-9]{0,12}([\d,]+\.?\d*)/i, /amount[^0-9]{0,12}([\d,]+\.?\d*)/i] },
    expected: ['rrspDeduction'],
  },
  FHSA: {
    label: 'FHSA Contribution Receipt',
    fields: { fhsaDeduction: [/contribution[^0-9]{0,12}([\d,]+\.?\d*)/i, /amount[^0-9]{0,12}([\d,]+\.?\d*)/i] },
    expected: ['fhsaDeduction'],
  },
  DONATION: {
    label: 'Charitable Donation Receipt',
    fields: { donations: [/(?:total|amount|donation)[^0-9]{0,12}([\d,]+\.?\d*)/i] },
    expected: ['donations'],
  },
  MEDICAL: {
    label: 'Medical Expense Receipts',
    fields: { medicalExpenses: [/(?:total|amount)[^0-9]{0,12}([\d,]+\.?\d*)/i] },
    expected: ['medicalExpenses'],
  },
  T5008: {
    label: 'T5008 — Securities Transactions',
    fields: { capitalGains: [/(?:gain|net gain)[^0-9]{0,12}(-?[\d,]+\.?\d*)/i] },
    expected: ['capitalGains'],
  },
  T4E: {
    label: 'T4E — Employment Insurance Benefits',
    fields: {
      otherIncome: [/box\s*14[^0-9]{0,12}([\d,]+\.?\d*)/i, /total benefits[^0-9]{0,12}([\d,]+\.?\d*)/i],
      taxWithheld: [/box\s*22[^0-9]{0,12}([\d,]+\.?\d*)/i],
    },
    expected: ['otherIncome'],
  },
  CHILDCARE: {
    label: 'Child Care Expense Receipt',
    fields: { childCare: [/(?:total|amount)[^0-9]{0,12}([\d,]+\.?\d*)/i] },
    expected: ['childCare'],
  },
};

const num = (s) => (s == null ? null : parseFloat(String(s).replace(/[, $]/g, '')));

/** Default OCR provider: passthrough for text, explicit error for binary. */
const defaultOcr = {
  toText(doc) {
    if (doc.text) return doc.text;
    throw new Error('No OCR provider configured for binary documents. Provide { text } or { fields }, or plug in an OCR provider.');
  },
};

/**
 * Extract normalized fields from a single document.
 * @returns {{type, label, fields, confidence, foundFields, missingExpected, status}}
 */
function extractDocument(doc, ocrProvider = defaultOcr) {
  const type = (doc.type || 'UNKNOWN').toUpperCase();
  const map = SLIP_MAP[type];
  if (!map) {
    return { type, label: 'Unrecognized document', fields: {}, confidence: 0, foundFields: [], missingExpected: [], status: 'unsupported' };
  }

  // Path 1: structured fields provided directly
  if (doc.fields && Object.keys(doc.fields).length) {
    const fields = {};
    for (const [k, v] of Object.entries(doc.fields)) {
      const val = num(v);
      if (val != null && !isNaN(val)) fields[k] = val;
    }
    const found = Object.keys(fields);
    const missing = map.expected.filter((e) => !(e in fields));
    return {
      type, label: map.label, fields,
      confidence: 0.99,
      foundFields: found, missingExpected: missing,
      status: missing.length ? 'needs_review' : 'processed',
    };
  }

  // Path 2: scan text (from OCR / PDF)
  const text = ocrProvider.toText(doc) || '';
  const fields = {};
  const found = [];
  for (const [field, patterns] of Object.entries(map.fields)) {
    for (const re of patterns) {
      const m = text.match(re);
      if (m && m[1] != null) {
        const val = num(m[1]);
        if (val != null && !isNaN(val)) { fields[field] = (fields[field] || 0) + val; found.push(field); break; }
      }
    }
  }
  const uniqFound = [...new Set(found)];
  const missing = map.expected.filter((e) => !(e in fields));
  // Confidence: share of expected boxes located, floored so a partial read still surfaces.
  const expectedHit = map.expected.length ? map.expected.filter((e) => e in fields).length / map.expected.length : (uniqFound.length ? 1 : 0);
  const confidence = Math.round((0.55 + 0.44 * expectedHit) * 100) / 100;
  return {
    type, label: map.label, fields,
    confidence: uniqFound.length ? confidence : 0,
    foundFields: uniqFound, missingExpected: missing,
    status: !uniqFound.length ? 'failed' : missing.length ? 'needs_review' : 'processed',
  };
}

/**
 * Scan a batch of documents and merge them into one financial profile.
 * @returns {{ financial, documents, summary }}
 */
function scanDocuments(docs = [], ocrProvider = defaultOcr) {
  const financial = {};
  const results = [];
  const ADDITIVE = new Set([
    'employmentIncome', 'selfEmploymentIncome', 'interestIncome', 'eligibleDividends',
    'nonEligibleDividends', 'capitalGains', 'pensionIncome', 'otherIncome',
    'rrspDeduction', 'fhsaDeduction', 'unionDues', 'childCare', 'movingExpenses',
    'employmentExpenses', 'otherDeductions', 'tuition', 'medicalExpenses', 'donations',
    'cppContrib', 'eiContrib', 'taxWithheld',
  ]);

  for (const doc of docs) {
    const res = extractDocument(doc, ocrProvider);
    for (const [k, v] of Object.entries(res.fields)) {
      if (ADDITIVE.has(k)) financial[k] = round2((financial[k] || 0) + v);
    }
    results.push({
      type: res.type, label: res.label, status: res.status,
      confidence: res.confidence, foundFields: res.foundFields,
      missingExpected: res.missingExpected,
      fields: res.fields, name: doc.name || res.label,
    });
  }

  const processed = results.filter((r) => r.status === 'processed').length;
  const needsReview = results.filter((r) => r.status === 'needs_review').length;
  const failed = results.filter((r) => r.status === 'failed' || r.status === 'unsupported').length;

  return {
    financial,
    documents: results,
    summary: {
      total: results.length, processed, needsReview, failed,
      avgConfidence: results.length
        ? Math.round((results.reduce((s, r) => s + r.confidence, 0) / results.length) * 100) / 100
        : 0,
      typesSeen: [...new Set(results.map((r) => r.type))],
    },
  };
}

const round2 = (n) => Math.round((n + Number.EPSILON) * 100) / 100;

module.exports = { extractDocument, scanDocuments, SLIP_MAP };
