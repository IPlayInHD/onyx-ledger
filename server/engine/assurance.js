/**
 * ONYX Intelligence — assurance & reliability framework
 * =========================================================================
 * This is the layer that makes the audit trustworthy instead of merely
 * plausible. It does for every return what a diligent tax accountant does
 * before signing off:
 *
 *   1. RECONCILIATION / CROSS-CHECKS  — re-derives payroll figures (CPP, EI,
 *      withholding) from first principles and reconciles them against the
 *      slips provided, ties out federal + provincial to the total, and flags
 *      anything a reviewer would question.
 *   2. COVERAGE & SCOPE  — compares what the user SAID about their situation
 *      (investments, rental, foreign, crypto, self-employment, student) with
 *      what was actually captured, and explicitly surfaces anything the engine
 *      does not model, so the number is never silently wrong.
 *   3. CONFIDENCE  — turns those checks + documentation quality + data-
 *      verification status into a single, honest reliability score for THIS
 *      user's audit, with the drivers shown. A sparse, unreconciled return
 *      scores low on purpose; a complete, fully-reconciled one scores high.
 *   4. ASSUMPTIONS LEDGER  — lists only the modelling assumptions that actually
 *      affect this user, so nothing is hidden.
 *
 * Design intent: reliability, consistency and disclosure. The framework is
 * deterministic and testable, and it is what lets the product present a
 * professional-grade analysis while remaining precise about its one hard
 * boundary — it computes and reconciles a return, it does not transmit one to
 * the CRA (that requires NETFILE certification).
 *
 * NOTE: helper names are prefixed `asu*` so this module concatenates cleanly
 * into the single-scope static browser bundle without colliding with other
 * engine modules' top-level `money` / `clamp`.
 * =========================================================================
 */

'use strict';

const { getTaxData } = require('./taxData');

const asuMoney = (n) => Math.round(n);
const asuCash = (n) => '$' + asuMoney(n).toLocaleString('en-CA');
const asuClamp = (n, lo = 0, hi = 100) => Math.max(lo, Math.min(hi, n));

/**
 * Auditor-style consistency & reconciliation checks.
 * @returns {Array<{id,status,label,detail}>} status ∈ 'pass' | 'review' | 'flag'
 */
function runConsistencyChecks(ret) {
  const p = ret.profile;
  const data = getTaxData(p.year);
  const fed = data.federal;
  const checks = [];
  const add = (id, status, label, detail) => checks.push({ id, status, label, detail });

  const emp = p.employmentIncome;

  // C0 — is there anything to audit at all?
  if (ret.income.total <= 0) {
    add('has-income', 'flag', 'No income to audit',
      'No income was captured. Add at least one income slip (T4, T4A, T5…) so the engine has something to reconcile.');
  }

  // C1 — CPP contribution reconciles with employment income
  if (emp > fed.cpp.exemption) {
    const base = Math.min(emp, fed.cpp.maxPensionable) - fed.cpp.exemption;
    const expected = asuClamp(base * fed.cpp.rate, 0, fed.cpp.max);
    const ceiling = fed.cpp.max + fed.cpp.cpp2.max;
    if (p.cppContrib === 0) {
      add('cpp', 'review', 'CPP contribution appears blank',
        `Employment income of ${asuCash(emp)} usually has CPP (T4 box 16). None was captured, so the CPP credit and reconciliation may be understated.`);
    } else if (p.cppContrib > ceiling + 2) {
      add('cpp', 'flag', 'CPP exceeds the annual maximum',
        `CPP of ${asuCash(p.cppContrib)} is above the ${asuCash(ceiling)} maximum — usually a sign of multiple employers. You can likely recover the over-contribution on your return.`);
    } else if (Math.abs(p.cppContrib - expected) <= Math.max(expected * 0.15, 60)) {
      add('cpp', 'pass', 'CPP reconciles with employment income',
        `Your CPP of ${asuCash(p.cppContrib)} matches the ~${asuCash(expected)} expected on ${asuCash(emp)} of income.`);
    } else {
      add('cpp', 'review', 'CPP differs from the expected amount',
        `CPP of ${asuCash(p.cppContrib)} differs from the ~${asuCash(expected)} expected on this income — check for multiple T4s or a data-entry error.`);
    }
  }

  // C2 — EI premium reconciles with employment income
  if (emp > 2000 && p.province !== 'QC') {
    const expected = asuClamp(Math.min(emp, fed.ei.maxInsurable) * fed.ei.rate, 0, fed.ei.max);
    if (p.eiContrib === 0) {
      add('ei', 'review', 'EI premium appears blank',
        `Most employees pay EI (T4 box 18). None was captured on ${asuCash(emp)} of employment income — verify your slip.`);
    } else if (p.eiContrib > fed.ei.max + 2) {
      add('ei', 'flag', 'EI exceeds the annual maximum',
        `EI of ${asuCash(p.eiContrib)} is above the ${asuCash(fed.ei.max)} maximum — often multiple employers. The over-payment is recoverable.`);
    } else if (Math.abs(p.eiContrib - expected) <= Math.max(expected * 0.2, 40)) {
      add('ei', 'pass', 'EI reconciles with employment income',
        `Your EI of ${asuCash(p.eiContrib)} matches the ~${asuCash(expected)} expected on this income.`);
    } else {
      add('ei', 'review', 'EI differs from the expected amount',
        `EI of ${asuCash(p.eiContrib)} differs from the ~${asuCash(expected)} expected — check box 18 across all T4s.`);
    }
  }

  // C3 — withholding plausibility
  if (emp > 20000) {
    if (p.taxWithheld === 0) {
      add('withholding', 'review', 'No income tax withheld',
        `No tax was withheld (T4 box 22) on ${asuCash(emp)} of employment income — unusual for a T4. Confirm the slip, or expect a balance owing.`);
    } else if (p.taxWithheld > ret.tax.total * 3 && p.taxWithheld > 3000) {
      add('withholding', 'review', 'Withholding looks unusually high',
        `Tax withheld (${asuCash(p.taxWithheld)}) is far above the estimated tax (${asuCash(ret.tax.total)}). Double-check box 22 and that no slip is entered twice.`);
    } else {
      add('withholding', 'pass', 'Withholding is consistent with income',
        `Tax withheld of ${asuCash(p.taxWithheld)} is in a normal range for this income and drives your ${ret.isRefund ? 'refund' : 'balance'}.`);
    }
  }

  // C4 — coverage: what you described vs. what was captured
  const investmentIncome = p.interestIncome + p.eligibleDividends + p.nonEligibleDividends + Math.max(0, p.capitalGains);
  if (p.hasInvestments && investmentIncome === 0) {
    add('cover-invest', 'review', 'Investments declared but none captured',
      'You indicated you hold investments, but no investment income (T5 / T3 / T5008) was captured — add those slips so nothing is missed.');
  }
  if (p.isStudent && p.tuition === 0) {
    add('cover-tuition', 'review', 'Student with no tuition captured',
      'You indicated you are a student, but no tuition (T2202) was captured — one of the most commonly missed credits.');
  }

  // C5 — rental income: now modelled (net rents). Reconcile declared vs entered.
  const rentalNet = (p.rentalIncome || 0) - (p.rentalExpenses || 0);
  if (p.hasRentalIncome || p.rentalIncome > 0) {
    if ((p.rentalIncome || 0) === 0 && (p.rentalExpenses || 0) === 0) {
      add('scope-rental', 'review', 'Rental declared but not entered',
        'You indicated rental income but none was entered. Add your gross rents and operating expenses so the net rental income is included.');
    } else {
      add('scope-rental', 'pass', 'Rental income is included',
        `Net rental income of ${asuCash(rentalNet)} is included as ordinary income. Capital cost allowance (depreciation) is optional and not auto-applied.`);
    }
  }

  // Self-employment: now modelled with self-employed CPP + business expenses.
  if (p.selfEmploymentIncome > 0) {
    add('scope-selfemp', p.selfEmploymentExpenses > 0 ? 'pass' : 'review', 'Self-employment is included',
      p.selfEmploymentExpenses > 0
        ? 'Net self-employment income and self-employed CPP (both employer and employee portions, Schedule 8) are modelled. GST/HST and capital cost allowance are not — confirm those separately.'
        : 'Self-employment income and self-employed CPP are modelled, but no business expenses were entered. If you have any (supplies, vehicle, home office), enter them — they reduce your income directly.');
  }

  // Still genuinely out of scope — disclosed, never silently dropped.
  if (p.hasForeignIncome) {
    add('scope-foreign', 'flag', 'Foreign income is not modelled',
      'Foreign income and the foreign tax credit are not modelled here and are excluded from the estimate.');
  }
  if (p.hasCrypto) {
    add('scope-crypto', 'review', 'Crypto dispositions need manual entry',
      'Gains and losses from crypto dispositions are not detected automatically — enter the net capital gain manually so it is included.');
  }

  // C6 — internal tie-out (federal + provincial reconcile to total; nothing negative)
  const tieOut = Math.abs((ret.tax.federal + ret.tax.provincial) - ret.tax.total) < 0.05;
  const nonNegative = ret.tax.federal >= 0 && ret.tax.provincial >= 0 && ret.credits.federalValue >= 0;
  if (tieOut && nonNegative) {
    add('tie-out', 'pass', 'The return balances',
      'Federal and provincial tax reconcile exactly to the total, and every tax and credit figure is non-negative — the arithmetic ties out.');
  } else {
    add('tie-out', 'flag', 'Internal reconciliation failed',
      'Federal + provincial tax did not reconcile to the total. This should never happen — do not rely on this result and report it.');
  }

  return checks;
}

/**
 * Modelling assumptions that actually affect THIS user's return.
 * @returns {Array<{text}>}
 */
function buildAssumptions(ret, engine) {
  const p = ret.profile;
  const a = [];
  a.push({ text: 'Net income is used as taxable income. Loss carryforwards, capital-loss carrybacks and specialized deductions are not applied.' });
  if (Math.max(0, p.capitalGains) > 0) a.push({ text: 'Capital gains use the 50% inclusion rate; capital-loss carryforwards are not modelled.' });
  if (p.eligibleDividends + p.nonEligibleDividends > 0) a.push({ text: 'The provincial dividend tax credit is approximated; the federal DTC is applied precisely.' });
  if (p.age && p.age >= 65) a.push({ text: 'The age amount is applied federally and approximated provincially.' });
  if (p.selfEmploymentIncome > 0) a.push({ text: 'Self-employment income is net of the business expenses you entered; self-employed CPP (both portions) is computed on Schedule 8. GST/HST and capital cost allowance (depreciation) are not modelled.' });
  if ((p.rentalIncome || 0) > 0 || (p.rentalExpenses || 0) > 0) a.push({ text: 'Rental income is included as gross rents minus the operating expenses you entered; capital cost allowance is optional and not auto-applied.' });
  if (engine && engine.federalVerified && !engine.provinceVerified && p.year === 2025) {
    a.push({ text: `2025 ${ret.provinceName} provincial rates are indexed estimates pending verification against the province's published amounts (federal figures are CRA-verified).` });
  }
  return a;
}

/**
 * Confidence for this specific audit, with the drivers that moved it.
 */
function computeConfidence(ret, health, checks, docSummary, engine) {
  let score = 100;
  const drivers = [];
  const flags = checks.filter((c) => c.status === 'flag');
  const reviews = checks.filter((c) => c.status === 'review');
  const passes = checks.filter((c) => c.status === 'pass');

  if (passes.length) drivers.push({ label: `${passes.length} reconciliation check${passes.length > 1 ? 's' : ''} passed`, delta: 0, tone: 'pos' });
  if (flags.length) { const d = -12 * flags.length; score += d; drivers.push({ label: `${flags.length} item${flags.length > 1 ? 's' : ''} to resolve before relying on this`, delta: d, tone: 'neg' }); }
  if (reviews.length) { const d = -6 * reviews.length; score += d; drivers.push({ label: `${reviews.length} figure${reviews.length > 1 ? 's' : ''} to double-check`, delta: d, tone: 'warn' }); }

  if (!docSummary || !docSummary.total) {
    score -= 15; drivers.push({ label: 'No source documents scanned — figures entered manually', delta: -15, tone: 'warn' });
  } else {
    if ((docSummary.avgConfidence || 0) < 0.75) { score -= 8; drivers.push({ label: 'Some documents read at lower confidence', delta: -8, tone: 'warn' }); }
    if (docSummary.failed > 0) { const d = -5 * docSummary.failed; score += d; drivers.push({ label: `${docSummary.failed} document(s) could not be read`, delta: d, tone: 'neg' }); }
    if (docSummary.avgConfidence >= 0.9 && docSummary.failed === 0) drivers.push({ label: 'All documents read cleanly', delta: 0, tone: 'pos' });
  }

  if (engine && engine.federalVerified && engine.provinceVerified) {
    drivers.push({ label: 'Federal & provincial constants verified against official sources', delta: 0, tone: 'pos' });
  } else if (engine && !engine.dataVerified) {
    score -= 5; drivers.push({ label: 'Some tax constants for this year are indexed estimates', delta: -5, tone: 'warn' });
  }

  score = asuClamp(Math.round(score), 0, 100);
  const band =
    score >= 85 ? { label: 'High confidence', tone: 'positive', blurb: 'Your inputs are complete and reconcile cleanly. This analysis is as reliable as the data you provided.' } :
    score >= 70 ? { label: 'Good confidence', tone: 'positive', blurb: 'A solid analysis. Resolve the noted items to tighten it further.' } :
    score >= 55 ? { label: 'Moderate confidence', tone: 'warning', blurb: 'Usable as a guide, but some figures need checking or documents are missing.' } :
                  { label: 'Preliminary', tone: 'critical', blurb: 'Add the missing slips and resolve the flags before relying on these numbers.' };

  return { score, band, drivers };
}

/**
 * Orchestrate the full assurance report for an audit.
 */
function buildAssurance(ret, health, docSummary, engine) {
  const checks = runConsistencyChecks(ret);
  const assumptions = buildAssumptions(ret, engine);
  const confidence = computeConfidence(ret, health, checks, docSummary, engine);
  const counts = {
    pass: checks.filter((c) => c.status === 'pass').length,
    review: checks.filter((c) => c.status === 'review').length,
    flag: checks.filter((c) => c.status === 'flag').length,
  };
  return {
    confidence,
    checks,
    counts,
    assumptions,
    method: {
      framework: 'CRA T1 General method — federal & provincial marginal brackets, non-refundable credit valuation, dividend gross-up & tax credit, 50% capital-gains inclusion, provincial surtax & health premium.',
      crossChecks: checks.length,
      reconciled: counts.flag === 0,
      engineVersion: engine ? engine.version : undefined,
      dataStatus: engine ? engine.dataStatus : undefined,
      dataVerified: engine ? engine.dataVerified : undefined,
    },
  };
}

module.exports = { buildAssurance, runConsistencyChecks, buildAssumptions, computeConfidence };
