/**
 * ONYX Intelligence — core Canadian tax engine
 * =========================================================================
 * Pure, deterministic calculation of a T1-style personal tax position from a
 * normalized financial profile. This is an AUDITOR-GRADE ESTIMATE, not a filed
 * return: it models the mainstream federal + provincial calculation (brackets,
 * the major non-refundable credits, dividend gross-up/DTC, capital-gains
 * inclusion, Ontario surtax + health premium, and the Quebec abatement).
 *
 * Deliberate simplifications (documented so advice is honest):
 *   - Uses net income as a proxy for taxable income (no capital-loss carryovers,
 *     stock-option or northern deductions, etc.).
 *   - Provincial non-refundable credits are modelled on the shared amounts
 *     (BPA, CPP/EI, tuition, medical, pension, spousal); province-specific
 *     amounts (age, disability variants) are approximated federally.
 *   - Provincial dividend tax credits are approximated; dividends are a small
 *     component for most filers. Figures are estimates, clearly labelled.
 * =========================================================================
 */

'use strict';

const { getTaxData, PROVINCE_NAMES } = require('./taxData');

const round = (n) => Math.round((n + Number.EPSILON) * 100) / 100;
const clampPos = (n) => (n > 0 ? n : 0);

/** Progressive tax on `income` given an array of {upTo, rate} brackets. */
function bracketTax(income, brackets) {
  let tax = 0;
  let lower = 0;
  for (const b of brackets) {
    if (income > lower) {
      tax += (Math.min(income, b.upTo) - lower) * b.rate;
      lower = b.upTo;
    } else break;
  }
  return tax;
}

/** Federal basic personal amount with the high-income phase-down. */
function federalBPA(netIncome, bpa) {
  if (netIncome <= bpa.phaseStart) return bpa.max;
  if (netIncome >= bpa.phaseEnd) return bpa.min;
  const frac = (netIncome - bpa.phaseStart) / (bpa.phaseEnd - bpa.phaseStart);
  return bpa.max - (bpa.max - bpa.min) * frac;
}

/** Ontario-style surtax on provincial tax after credits. */
function applySurtax(provTax, surtax) {
  if (!surtax) return 0;
  let s = 0;
  for (const tier of surtax) if (provTax > tier.over) s += (provTax - tier.over) * tier.rate;
  return s;
}

function healthPremium(taxableIncome, table) {
  if (!table) return 0;
  for (const band of table) if (taxableIncome <= band.upTo) return band.amount;
  return 0;
}

/**
 * Normalize a raw financial profile into every field the engine expects,
 * defaulting anything missing to 0 / sensible values.
 */
function normalizeProfile(p = {}) {
  // Coerce to a finite number (bad input -> 0), and a non-negative variant so a
  // stray negative slip value can never invert the tax math. Amounts are also
  // capped to a sane maximum to keep the engine stable on absurd input.
  const CAP = 1e9;
  const num = (v) => { const x = typeof v === 'number' ? v : parseFloat(v); return isFinite(x) ? Math.max(-CAP, Math.min(CAP, x)) : 0; };
  const n = num;
  const nn = (v) => Math.max(0, num(v));
  return {
    year: p.year || 2024,
    province: (p.province || 'ON').toUpperCase(),
    age: (() => { const a = nn(p.age); return a > 0 && a < 130 ? Math.round(a) : null; })(),
    maritalStatus: p.maritalStatus || 'single',
    spouseNetIncome: nn(p.spouseNetIncome),
    dependants: Math.min(20, Math.round(nn(p.dependants))),
    isStudent: !!p.isStudent,
    disability: !!p.disability,
    firstTimeHomeBuyer: !!p.firstTimeHomeBuyer,
    ownsHome: !!p.ownsHome,
    // context flags (do not affect the tax calc; drive the checklist & calendar)
    employmentType: p.employmentType || 'employed',
    hasInvestments: !!p.hasInvestments,
    hasRentalIncome: !!p.hasRentalIncome,
    hasForeignIncome: !!p.hasForeignIncome,
    hasCrypto: !!p.hasCrypto,
    // income (non-negative; capital gains may be a net loss, so it stays signed)
    employmentIncome: nn(p.employmentIncome),
    selfEmploymentIncome: nn(p.selfEmploymentIncome),
    interestIncome: nn(p.interestIncome),
    eligibleDividends: nn(p.eligibleDividends),
    nonEligibleDividends: nn(p.nonEligibleDividends),
    capitalGains: n(p.capitalGains), // actual gain/loss; engine applies inclusion rate
    pensionIncome: nn(p.pensionIncome),
    otherIncome: nn(p.otherIncome),
    // self-employment business expenses (reduce gross self-employment income)
    selfEmploymentExpenses: nn(p.selfEmploymentExpenses),
    // rental (gross rents + operating expenses; net can be a deductible loss)
    rentalIncome: nn(p.rentalIncome),
    rentalExpenses: nn(p.rentalExpenses),
    // deductions
    rrspDeduction: nn(p.rrspDeduction),
    fhsaDeduction: nn(p.fhsaDeduction),
    unionDues: nn(p.unionDues),
    childCare: nn(p.childCare),
    movingExpenses: nn(p.movingExpenses),
    employmentExpenses: nn(p.employmentExpenses),
    otherDeductions: nn(p.otherDeductions),
    // credits / receipts
    tuition: nn(p.tuition),
    medicalExpenses: nn(p.medicalExpenses),
    donations: nn(p.donations),
    // withheld / contributed
    cppContrib: nn(p.cppContrib),
    eiContrib: nn(p.eiContrib),
    taxWithheld: nn(p.taxWithheld),
    // planning context (used by advisory)
    rrspRoom: p.rrspRoom == null ? null : nn(p.rrspRoom),
    tfsaRoom: p.tfsaRoom == null ? null : nn(p.tfsaRoom),
  };
}

function donationCredit(donations, rate) {
  if (donations <= 0) return 0;
  const first = Math.min(donations, rate.threshold) * rate.low;
  const rest = Math.max(0, donations - rate.threshold) * rate.high;
  return first + rest;
}

/**
 * Compute the full tax position. Returns a rich breakdown object.
 */
function computeReturn(rawProfile) {
  const p = normalizeProfile(rawProfile);
  const data = getTaxData(p.year);
  const fed = data.federal;
  const prov = data.provinces[p.province] || data.provinces.ON;

  // ---- Income ----
  const taxableCapitalGains = p.capitalGains * fed.capitalGainsInclusion;
  const grossedElig = p.eligibleDividends * (1 + fed.eligibleDiv.grossUp);
  const grossedNonElig = p.nonEligibleDividends * (1 + fed.nonEligibleDiv.grossUp);
  // Self-employment net of business expenses; rental net of operating expenses.
  // Both are ordinary income and may be a deductible loss (kept signed).
  const netSelfEmployment = p.selfEmploymentIncome - p.selfEmploymentExpenses;
  const netRental = p.rentalIncome - p.rentalExpenses;

  // ---- Self-employed CPP (Schedule 8) ----
  // The self-employed pay BOTH the employee and employer halves (2× the rate) on
  // the pensionable earnings not already covered by employment. Half is a
  // deduction (line 22200); half earns the CPP credit like an employee's share.
  const cppCeiling = fed.cpp.max + fed.cpp.cpp2.max;
  const empCppBase = clampPos(Math.min(p.employmentIncome, fed.cpp.maxPensionable) - fed.cpp.exemption);
  const totCppBase = clampPos(Math.min(p.employmentIncome + clampPos(netSelfEmployment), fed.cpp.maxPensionable) - fed.cpp.exemption);
  const seCppBase = clampPos(totCppBase - empCppBase);
  const seCppTotal = round(seCppBase * fed.cpp.rate * 2);
  const seCppDeduction = round(seCppTotal / 2);
  const seCppCredit = seCppTotal / 2;

  const totalIncome =
    p.employmentIncome + netSelfEmployment + p.interestIncome +
    grossedElig + grossedNonElig + taxableCapitalGains + p.pensionIncome + p.otherIncome + netRental;

  // ---- Deductions ----
  const totalDeductions =
    p.rrspDeduction + p.fhsaDeduction + p.unionDues + p.childCare +
    p.movingExpenses + p.employmentExpenses + p.otherDeductions + seCppDeduction;

  const netIncome = clampPos(totalIncome - totalDeductions);
  const taxableIncome = netIncome; // documented proxy

  // ---- Shared non-refundable credit amounts ----
  // Employee CPP contributions plus the employee-equivalent half of self-employed CPP.
  const employeeCpp = Math.min(p.cppContrib, cppCeiling);
  const cpp = Math.min(p.cppContrib + seCppCredit, cppCeiling);
  const ei = Math.min(p.eiContrib, fed.ei.max);
  const canadaEmployment = p.employmentIncome > 0 ? Math.min(fed.canadaEmployment, p.employmentIncome) : 0;
  const pensionCredit = Math.min(fed.pensionIncomeMax, p.pensionIncome);
  const ageAmount =
    p.age && p.age >= 65
      ? clampPos(fed.ageAmount.max - clampPos(netIncome - fed.ageAmount.threshold) * fed.ageAmount.rate)
      : 0;
  const disabilityAmount = p.disability ? fed.disabilityAmount : 0;
  const spousalAmount =
    (p.maritalStatus === 'married' || p.maritalStatus === 'commonlaw')
      ? clampPos(fed.bpa.max - p.spouseNetIncome)
      : 0;
  const medicalThreshold = Math.min(netIncome * fed.medical.pct, fed.medical.cap);
  const medicalEligible = clampPos(p.medicalExpenses - medicalThreshold);

  // ---- FEDERAL tax ----
  const fedTaxBefore = bracketTax(taxableIncome, fed.brackets);
  const fedBpaAmt = federalBPA(netIncome, fed.bpa);
  const fedCreditBase =
    fedBpaAmt + cpp + ei + canadaEmployment + p.tuition + ageAmount +
    pensionCredit + disabilityAmount + spousalAmount + medicalEligible;
  const fedNonRefundable = fed.creditRate * fedCreditBase + donationCredit(p.donations, fed.donation);
  const fedDTC = grossedElig * fed.eligibleDiv.dtc + grossedNonElig * fed.nonEligibleDiv.dtc;
  let federalTax = clampPos(fedTaxBefore - fedNonRefundable - fedDTC);
  if (prov.abatement) federalTax *= 1 - prov.abatement; // Quebec

  // ---- PROVINCIAL tax ----
  const provTaxBefore = bracketTax(taxableIncome, prov.brackets);
  // Provinces mirror the shared credits but NOT the federal Canada employment amount.
  const provCreditBase =
    prov.bpa + cpp + ei + p.tuition + pensionCredit +
    (spousalAmount > 0 ? Math.min(spousalAmount, prov.bpa) : 0) + medicalEligible;
  const provDonation =
    donations2(p.donations, prov.creditRate, prov.brackets[prov.brackets.length - 1].rate);
  const provNonRefundable = prov.creditRate * provCreditBase + provDonation;
  let provincialTax = clampPos(provTaxBefore - provNonRefundable);
  const surtax = applySurtax(provincialTax, prov.surtax);
  provincialTax += surtax;
  const ohp = healthPremium(taxableIncome, prov.healthPremium);
  provincialTax += ohp;

  // ---- Totals ----
  const incomeTax = round(federalTax + provincialTax);
  // Self-employed CPP is payable with the return (line 42100), on top of income tax.
  const cppPayableSE = seCppTotal;
  const totalPayable = round(incomeTax + cppPayableSE);
  const refundOrBalance = round(p.taxWithheld - totalPayable); // + = refund, - = owing

  // ---- Rates ----
  const totalTaxFn = (ti) =>
    bracketTax(ti, fed.brackets) * (prov.abatement ? 1 - prov.abatement : 1) +
    bracketTax(ti, prov.brackets) +
    applySurtax(clampPos(bracketTax(ti, prov.brackets) - prov.creditRate * prov.bpa), prov.surtax);
  const marginalRate = round((totalTaxFn(taxableIncome + 1000) - totalTaxFn(taxableIncome)) / 1000 * 100) / 100;
  const averageRate = totalIncome > 0 ? round((incomeTax / totalIncome) * 100) / 100 : 0;

  // Federal bracket position (for visualization) + cash-flow breakdown.
  let bracketFederal = null; let lo = 0;
  for (const b of fed.brackets) {
    if (taxableIncome <= b.upTo) { bracketFederal = { rate: b.rate, from: lo, upTo: b.upTo === Infinity ? null : b.upTo, toNext: b.upTo === Infinity ? null : round(b.upTo - taxableIncome) }; break; }
    lo = b.upTo;
  }
  const cppCash = round(employeeCpp + cppPayableSE); // actual CPP out of pocket
  const takeHome = round(totalIncome - incomeTax - cppCash - ei);
  const cashflow = {
    gross: round(totalIncome),
    federalTax: round(federalTax),
    provincialTax: round(provincialTax),
    cpp: cppCash, ei: round(ei),
    takeHome,
    takeHomePct: totalIncome > 0 ? round((takeHome / totalIncome) * 100) / 100 : 0,
  };

  return {
    year: p.year,
    province: p.province,
    provinceName: PROVINCE_NAMES[p.province] || p.province,
    income: {
      employment: round(p.employmentIncome),
      selfEmployment: round(netSelfEmployment),
      interest: round(p.interestIncome),
      eligibleDividendsGrossed: round(grossedElig),
      nonEligibleDividendsGrossed: round(grossedNonElig),
      taxableCapitalGains: round(taxableCapitalGains),
      pension: round(p.pensionIncome),
      rental: round(netRental),
      other: round(p.otherIncome),
      total: round(totalIncome),
    },
    deductions: {
      rrsp: round(p.rrspDeduction), fhsa: round(p.fhsaDeduction), unionDues: round(p.unionDues),
      childCare: round(p.childCare), moving: round(p.movingExpenses),
      employment: round(p.employmentExpenses), selfEmployedCpp: seCppDeduction,
      other: round(p.otherDeductions),
      total: round(totalDeductions),
    },
    netIncome: round(netIncome),
    taxableIncome: round(taxableIncome),
    credits: {
      basicPersonalAmount: round(fedBpaAmt),
      cppEi: round(cpp + ei),
      canadaEmployment: round(canadaEmployment),
      tuition: round(p.tuition),
      medicalEligible: round(medicalEligible),
      medicalThreshold: round(medicalThreshold),
      ageAmount: round(ageAmount),
      pension: round(pensionCredit),
      disability: round(disabilityAmount),
      spousal: round(spousalAmount),
      donations: round(p.donations),
      federalValue: round(fedNonRefundable + fedDTC),
      provincialValue: round(provNonRefundable),
    },
    tax: {
      federalBeforeCredits: round(fedTaxBefore),
      federal: round(federalTax),
      provincialBeforeCredits: round(provTaxBefore),
      surtax: round(surtax),
      healthPremium: round(ohp),
      provincial: round(provincialTax),
      total: incomeTax,
      cppPayableSE: round(cppPayableSE),
      totalPayable,
    },
    taxWithheld: round(p.taxWithheld),
    refundOrBalance,
    isRefund: refundOrBalance >= 0,
    marginalRate,
    averageRate,
    bracketFederal,
    cashflow,
    profile: p,
  };
}

// Provincial donation credit: lowest rate on first $200, top provincial rate above.
function donations2(donations, lowRate, topRate) {
  if (donations <= 0) return 0;
  const first = Math.min(donations, 200) * lowRate;
  const rest = Math.max(0, donations - 200) * topRate;
  return first + rest;
}

module.exports = { computeReturn, normalizeProfile, bracketTax, round };
