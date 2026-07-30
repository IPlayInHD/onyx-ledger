/**
 * ONYX Intelligence — engine entry point
 * Orchestrates: scan documents → compute return → score health →
 * find opportunities → build advisory. One call: runAudit().
 */

'use strict';

const { computeReturn, normalizeProfile } = require('./taxEngine');
const { scanDocuments } = require('./extract');
const { computeTaxHealth } = require('./scoring');
const { findOpportunities, whatWeFound, buildAdvisory } = require('./advisory');
const { buildChecklist } = require('./checklist');
const { optimizeRRSP, accountPriority, estimateBenefits, taxCalendar } = require('./planner');
const { getTaxData, PROVINCE_NAMES } = require('./taxData');

/**
 * Run a full audit.
 * @param {object} args
 * @param {object} args.profile     demographic + planning context (province, age, marital, etc.)
 * @param {object} [args.financial] pre-normalized financial figures (optional)
 * @param {Array}  [args.documents] raw documents to scan ({type, fields|text})
 * @param {number} [args.year]
 * @param {object} [args.ocrProvider]
 * @returns {object} full audit report
 */
function runAudit({ profile = {}, financial = {}, documents = [], year, ocrProvider } = {}) {
  const taxYear = year || profile.year || 2024;

  // 1) Scan documents into financial figures, merged with any supplied figures.
  const scan = scanDocuments(documents, ocrProvider);
  const mergedFinancial = mergeFinancial(financial, scan.financial);

  // 2) Compute the return.
  const input = Object.assign({}, profile, mergedFinancial, { year: taxYear });
  const ret = computeReturn(input);
  ret._docSummary = scan.summary; // let scoring see documentation quality

  // 3) Score, find opportunities, compose advisory.
  const health = computeTaxHealth(ret);
  const opportunities = findOpportunities(ret, health);
  const found = whatWeFound(ret, scan.summary);
  const advisory = buildAdvisory(ret, health, opportunities);

  // 4) Personalized checklist + planning tools (QOL).
  const checklist = buildChecklist(ret, documents);
  const rrspMoves = optimizeRRSP(ret, health.context.estimatedRrspRoom);
  const accounts = accountPriority(ret);
  const benefits = estimateBenefits(ret);
  const calendar = taxCalendar(ret.profile);

  return {
    generatedAt: new Date().toISOString(),
    taxYear,
    province: ret.province,
    provinceName: ret.provinceName,
    return: stripInternal(ret),
    health,
    documents: scan.documents,
    documentSummary: scan.summary,
    found,
    opportunities,
    advisory,
    checklist,
    rrspMoves,
    accounts,
    benefits,
    calendar,
  };
}

/**
 * Fast "what-if" recomputation for the live planner. Applies overrides on top
 * of the user's real profile + documents and returns a compact position.
 */
function simulate({ profile = {}, financial = {}, documents = [], year, overrides = {} } = {}) {
  const taxYear = year || profile.year || 2024;
  const scan = scanDocuments(documents);
  const merged = mergeFinancial(financial, scan.financial);
  const input = Object.assign({}, profile, merged, { year: taxYear });
  input.rrspDeduction = (input.rrspDeduction || 0) + (+overrides.rrsp || 0);
  input.fhsaDeduction = (input.fhsaDeduction || 0) + (+overrides.fhsa || 0);
  input.donations = (input.donations || 0) + (+overrides.donations || 0);
  input.employmentIncome = (input.employmentIncome || 0) + (+overrides.extraIncome || 0);
  input.capitalGains = (input.capitalGains || 0) + (+overrides.capitalGains || 0);
  const ret = computeReturn(input);
  return {
    refundOrBalance: ret.refundOrBalance, isRefund: ret.isRefund,
    marginalRate: ret.marginalRate, averageRate: ret.averageRate,
    taxableIncome: ret.taxableIncome, totalTax: ret.tax.total,
    bracketFederal: ret.bracketFederal, cashflow: ret.cashflow,
  };
}

function mergeFinancial(a = {}, b = {}) {
  const out = Object.assign({}, a);
  for (const [k, v] of Object.entries(b)) {
    if (typeof v === 'number') out[k] = (typeof out[k] === 'number' ? out[k] : 0) + v;
  }
  return out;
}

function stripInternal(ret) {
  const { _docSummary, profile, ...rest } = ret;
  return Object.assign({}, rest, { profile });
}

module.exports = {
  runAudit,
  simulate,
  computeReturn,
  normalizeProfile,
  scanDocuments,
  computeTaxHealth,
  findOpportunities,
  buildAdvisory,
  getTaxData,
  PROVINCE_NAMES,
};
