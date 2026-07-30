/* ONYX Ledger — static build (engine runs in the browser; data in localStorage). */
(function(){
'use strict';

/* ===== engine/taxData.js ===== */
/**
 * ONYX Intelligence — Canadian tax constants
 * =========================================================================
 * Tax year: 2024 (the constants a real engine maintains as data, separate
 * from the calculation logic in taxEngine.js).
 *
 * IMPORTANT — these figures are indexed and legislated ANNUALLY. They reflect
 * the 2024 federal and provincial parameters to the best of published CRA and
 * provincial finance references and MUST be validated against official CRA
 * (T1 General / income tax package) and each province's finance publications
 * before any production or advice use. The engine is written so a new tax year
 * is added purely as data — no logic changes.
 *
 * Sources to verify against each year:
 *   - CRA "Federal income tax rates" and the T1 income tax and benefit guide
 *   - Each province/territory's income tax rates and tax credit amounts
 *   - CRA "Indexation" adjustment factors
 * =========================================================================
 */


const FEDERAL_2024 = {
  brackets: [
    { upTo: 55867, rate: 0.15 },
    { upTo: 111733, rate: 0.205 },
    { upTo: 173205, rate: 0.26 },
    { upTo: 246752, rate: 0.29 },
    { upTo: Infinity, rate: 0.33 },
  ],
  // Basic personal amount phases DOWN for high earners (2024: $15,705 → $14,156).
  bpa: { max: 15705, min: 14156, phaseStart: 173205, phaseEnd: 246752 },
  creditRate: 0.15, // rate at which most federal non-refundable credits are valued
  canadaEmployment: 1433, // Canada employment amount (2024)
  pensionIncomeMax: 2000, // pension income amount
  ageAmount: { max: 8790, threshold: 44325, rate: 0.15 }, // 65+ age amount, clawed back
  // Canada / Quebec Pension Plan (employee side, 2024)
  cpp: {
    maxPensionable: 68500,
    exemption: 3500,
    rate: 0.0595,
    max: 3867.5,
    cpp2: { lower: 68500, upper: 73200, rate: 0.04, max: 188 }, // second additional CPP
  },
  // Employment Insurance (2024, outside Quebec)
  ei: { maxInsurable: 63200, rate: 0.0166, max: 1049.12 },
  medical: { pct: 0.03, cap: 2759 }, // threshold = lesser of 3% of net income or cap
  donation: { threshold: 200, low: 0.15, high: 0.29, top: 0.33, topBracket: 246752 },
  eligibleDiv: { grossUp: 0.38, dtc: 0.150198 }, // eligible dividend gross-up + federal DTC
  nonEligibleDiv: { grossUp: 0.15, dtc: 0.090301 },
  capitalGainsInclusion: 0.5, // 50% inclusion rate (2024)
  disabilityAmount: 9872, // disability tax credit base amount (2024)
  // Canada Workers Benefit (basic, single, 2024 — approximate, varies by province)
  cwb: { maxSingle: 1518, maxFamily: 2616, phaseInStart: 3000, singlePhaseOut: 24975, familyPhaseOut: 28494, phaseOutRate: 0.12 },
  gstCredit: { single: 340, perChild: 179, base: 349 }, // approximate annual GST/HST credit (2024 base year)
  rrspRoomRate: 0.18, // 18% of prior-year earned income
  rrspRoomCap: 31560, // 2024 RRSP dollar limit
  fhsaAnnual: 8000, // First Home Savings Account annual limit
  tfsaAnnual: 7000, // 2024 TFSA annual limit
};

/**
 * Provincial / territorial parameters (2024).
 * creditRate = the province's lowest bracket rate (how its non-refundable
 * credits are valued). Optional: surtax, healthPremium (Ontario).
 */
const PROVINCES_2024 = {
  ON: {
    name: 'Ontario',
    brackets: [
      { upTo: 51446, rate: 0.0505 },
      { upTo: 102894, rate: 0.0915 },
      { upTo: 150000, rate: 0.1116 },
      { upTo: 220000, rate: 0.1216 },
      { upTo: Infinity, rate: 0.1316 },
    ],
    bpa: 12399,
    creditRate: 0.0505,
    // Ontario surtax applies to Ontario tax after credits
    surtax: [ { over: 5554, rate: 0.20 }, { over: 7108, rate: 0.36 } ],
    // Ontario Health Premium by taxable income band (annual $)
    healthPremium: [
      { upTo: 20000, amount: 0 }, { upTo: 36000, amount: 300 }, { upTo: 48000, amount: 450 },
      { upTo: 72000, amount: 600 }, { upTo: 200000, amount: 750 }, { upTo: Infinity, amount: 900 },
    ],
  },
  BC: {
    name: 'British Columbia',
    brackets: [
      { upTo: 47937, rate: 0.0506 }, { upTo: 95875, rate: 0.077 }, { upTo: 110076, rate: 0.105 },
      { upTo: 133664, rate: 0.1229 }, { upTo: 181232, rate: 0.147 }, { upTo: 252752, rate: 0.168 },
      { upTo: Infinity, rate: 0.205 },
    ],
    bpa: 12580, creditRate: 0.0506,
  },
  AB: {
    name: 'Alberta',
    brackets: [
      { upTo: 148269, rate: 0.10 }, { upTo: 177922, rate: 0.12 }, { upTo: 237230, rate: 0.13 },
      { upTo: 355845, rate: 0.14 }, { upTo: Infinity, rate: 0.15 },
    ],
    bpa: 21885, creditRate: 0.10,
  },
  QC: {
    name: 'Quebec',
    brackets: [
      { upTo: 51780, rate: 0.14 }, { upTo: 103545, rate: 0.19 }, { upTo: 126000, rate: 0.24 },
      { upTo: Infinity, rate: 0.2575 },
    ],
    bpa: 18056, creditRate: 0.14,
    abatement: 0.165, // Quebec residents get a 16.5% federal tax abatement
  },
  MB: {
    name: 'Manitoba',
    brackets: [ { upTo: 47000, rate: 0.108 }, { upTo: 100000, rate: 0.1275 }, { upTo: Infinity, rate: 0.174 } ],
    bpa: 15780, creditRate: 0.108,
  },
  SK: {
    name: 'Saskatchewan',
    brackets: [ { upTo: 52057, rate: 0.105 }, { upTo: 148734, rate: 0.125 }, { upTo: Infinity, rate: 0.145 } ],
    bpa: 18491, creditRate: 0.105,
  },
  NS: {
    name: 'Nova Scotia',
    brackets: [
      { upTo: 29590, rate: 0.0879 }, { upTo: 59180, rate: 0.1495 }, { upTo: 93000, rate: 0.1667 },
      { upTo: 150000, rate: 0.175 }, { upTo: Infinity, rate: 0.21 },
    ],
    bpa: 8481, creditRate: 0.0879,
  },
  NB: {
    name: 'New Brunswick',
    brackets: [
      { upTo: 49958, rate: 0.094 }, { upTo: 99916, rate: 0.14 }, { upTo: 185064, rate: 0.16 },
      { upTo: Infinity, rate: 0.195 },
    ],
    bpa: 13044, creditRate: 0.094,
  },
  PE: {
    name: 'Prince Edward Island',
    brackets: [
      { upTo: 32656, rate: 0.0965 }, { upTo: 64313, rate: 0.1363 }, { upTo: 105000, rate: 0.1665 },
      { upTo: 140000, rate: 0.18 }, { upTo: Infinity, rate: 0.1875 },
    ],
    bpa: 13500, creditRate: 0.0965,
  },
  NL: {
    name: 'Newfoundland and Labrador',
    brackets: [
      { upTo: 43198, rate: 0.087 }, { upTo: 86395, rate: 0.145 }, { upTo: 154244, rate: 0.158 },
      { upTo: 215943, rate: 0.178 }, { upTo: 275870, rate: 0.198 }, { upTo: 551739, rate: 0.208 },
      { upTo: 1103478, rate: 0.213 }, { upTo: Infinity, rate: 0.218 },
    ],
    bpa: 10818, creditRate: 0.087,
  },
  YT: {
    name: 'Yukon',
    brackets: [
      { upTo: 55867, rate: 0.064 }, { upTo: 111733, rate: 0.09 }, { upTo: 173205, rate: 0.109 },
      { upTo: 500000, rate: 0.128 }, { upTo: Infinity, rate: 0.15 },
    ],
    bpa: 15705, creditRate: 0.064,
  },
  NT: {
    name: 'Northwest Territories',
    brackets: [
      { upTo: 50597, rate: 0.059 }, { upTo: 101198, rate: 0.086 }, { upTo: 164525, rate: 0.122 },
      { upTo: Infinity, rate: 0.1405 },
    ],
    bpa: 17373, creditRate: 0.059,
  },
  NU: {
    name: 'Nunavut',
    brackets: [
      { upTo: 53268, rate: 0.04 }, { upTo: 106537, rate: 0.07 }, { upTo: 173205, rate: 0.09 },
      { upTo: Infinity, rate: 0.115 },
    ],
    bpa: 18767, creditRate: 0.04,
  },
};

/**
 * 2025 tax year. Federal figures are the published 2025 amounts. Provincial
 * figures are the 2024 tables indexed forward (~2.8%) as a PRELIMINARY estimate,
 * with Alberta's new 2025 8% bracket applied explicitly. Verify provincial 2025
 * figures against each province's published amounts before production use.
 */
const FEDERAL_2025 = {
  brackets: [
    { upTo: 57375, rate: 0.15 }, { upTo: 114750, rate: 0.205 }, { upTo: 177882, rate: 0.26 },
    { upTo: 253414, rate: 0.29 }, { upTo: Infinity, rate: 0.33 },
  ],
  bpa: { max: 16129, min: 14538, phaseStart: 177882, phaseEnd: 253414 },
  creditRate: 0.15,
  canadaEmployment: 1471,
  pensionIncomeMax: 2000,
  ageAmount: { max: 9028, threshold: 45522, rate: 0.15 },
  cpp: { maxPensionable: 71300, exemption: 3500, rate: 0.0595, max: 4034.1, cpp2: { lower: 71300, upper: 81200, rate: 0.04, max: 396 } },
  ei: { maxInsurable: 65700, rate: 0.0164, max: 1077.48 },
  medical: { pct: 0.03, cap: 2834 },
  donation: { threshold: 200, low: 0.15, high: 0.29, top: 0.33, topBracket: 253414 },
  eligibleDiv: { grossUp: 0.38, dtc: 0.150198 },
  nonEligibleDiv: { grossUp: 0.15, dtc: 0.090301 },
  capitalGainsInclusion: 0.5,
  disabilityAmount: 10138,
  cwb: { maxSingle: 1590, maxFamily: 2739, phaseInStart: 3000, singlePhaseOut: 26149, familyPhaseOut: 29833, phaseOutRate: 0.12 },
  gstCredit: { single: 349, perChild: 184, base: 358 },
  rrspRoomRate: 0.18,
  rrspRoomCap: 32490,
  fhsaAnnual: 8000,
  tfsaAnnual: 7000,
};

function indexProvinces(base, factor) {
  const out = {};
  for (const [code, p] of Object.entries(base)) {
    const np = Object.assign({}, p, {
      brackets: p.brackets.map((b) => ({ upTo: b.upTo === Infinity ? Infinity : Math.round(b.upTo * factor), rate: b.rate })),
      bpa: Math.round(p.bpa * factor),
    });
    if (p.surtax) np.surtax = p.surtax.map((s) => ({ over: Math.round(s.over * factor), rate: s.rate }));
    out[code] = np;
  }
  return out;
}
const PROVINCES_2025 = indexProvinces(PROVINCES_2024, 1.028);
// Alberta 2025: new 8% bracket on the first $60,000 (credits still valued at 10%).
PROVINCES_2025.AB = {
  name: 'Alberta',
  brackets: [
    { upTo: 60000, rate: 0.08 }, { upTo: 151234, rate: 0.10 }, { upTo: 181481, rate: 0.12 },
    { upTo: 241974, rate: 0.13 }, { upTo: 362961, rate: 0.14 }, { upTo: Infinity, rate: 0.15 },
  ],
  bpa: 22323, creditRate: 0.10,
};

const TAX_DATA = {
  2024: { federal: FEDERAL_2024, provinces: PROVINCES_2024 },
  2025: { federal: FEDERAL_2025, provinces: PROVINCES_2025 },
};

const AVAILABLE_YEARS = [2025, 2024];

const PROVINCE_NAMES = Object.fromEntries(
  Object.entries(PROVINCES_2024).map(([code, p]) => [code, p.name])
);

function getTaxData(year) {
  return TAX_DATA[year] || TAX_DATA[2024];
}


/* ===== engine/taxEngine.js ===== */
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

  const totalIncome =
    p.employmentIncome + p.selfEmploymentIncome + p.interestIncome +
    grossedElig + grossedNonElig + taxableCapitalGains + p.pensionIncome + p.otherIncome;

  // ---- Deductions ----
  const totalDeductions =
    p.rrspDeduction + p.fhsaDeduction + p.unionDues + p.childCare +
    p.movingExpenses + p.employmentExpenses + p.otherDeductions;

  const netIncome = clampPos(totalIncome - totalDeductions);
  const taxableIncome = netIncome; // documented proxy

  // ---- Shared non-refundable credit amounts ----
  const cpp = Math.min(p.cppContrib, fed.cpp.max + fed.cpp.cpp2.max);
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
  const refundOrBalance = round(p.taxWithheld - incomeTax); // + = refund, - = owing

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
  const cpp2 = 0; // (CPP is already summed in `cpp`)
  const takeHome = round(totalIncome - incomeTax - cpp - ei);
  const cashflow = {
    gross: round(totalIncome),
    federalTax: round(federalTax),
    provincialTax: round(provincialTax),
    cpp: round(cpp), ei: round(ei),
    takeHome,
    takeHomePct: totalIncome > 0 ? round((takeHome / totalIncome) * 100) / 100 : 0,
  };

  return {
    year: p.year,
    province: p.province,
    provinceName: PROVINCE_NAMES[p.province] || p.province,
    income: {
      employment: round(p.employmentIncome),
      selfEmployment: round(p.selfEmploymentIncome),
      interest: round(p.interestIncome),
      eligibleDividendsGrossed: round(grossedElig),
      nonEligibleDividendsGrossed: round(grossedNonElig),
      taxableCapitalGains: round(taxableCapitalGains),
      pension: round(p.pensionIncome),
      other: round(p.otherIncome),
      total: round(totalIncome),
    },
    deductions: {
      rrsp: round(p.rrspDeduction), fhsa: round(p.fhsaDeduction), unionDues: round(p.unionDues),
      childCare: round(p.childCare), moving: round(p.movingExpenses),
      employment: round(p.employmentExpenses), other: round(p.otherDeductions),
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


/* ===== engine/extract.js ===== */
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


/* ===== engine/scoring.js ===== */
/**
 * ONYX Intelligence — Tax Health scoring
 * =========================================================================
 * A 0–100 score that mirrors how a tax professional would rate a filer's
 * position, across five weighted categories. Each sub-score is derived from
 * the computed return + profile + scanned documents, then combined.
 *
 * This is an educational assessment of tax *optimization and hygiene*, not an
 * audit-risk score or a compliance guarantee.
 * =========================================================================
 */



const clamp = (n, lo = 0, hi = 100) => Math.max(lo, Math.min(hi, n));
const pct = (n) => Math.round(clamp(n));

/** Estimate RRSP room if the user hasn't supplied it (18% of earned income, capped). */
function estimateRrspRoom(profile, data) {
  const earned = profile.employmentIncome + profile.selfEmploymentIncome;
  return Math.min(earned * data.federal.rrspRoomRate, data.federal.rrspRoomCap);
}

function computeTaxHealth(ret) {
  const p = ret.profile;
  const data = getTaxData(p.year);
  const notes = [];

  // 1) Registered savings utilization (RRSP / TFSA / FHSA) ---------------- 30
  const estRoom = p.rrspRoom != null ? p.rrspRoom + p.rrspDeduction : estimateRrspRoom(p, data);
  const rrspUtil = estRoom > 0 ? p.rrspDeduction / estRoom : (p.employmentIncome + p.selfEmploymentIncome > 0 ? 0 : 1);
  let savings = rrspUtil * 70;
  // FHSA opportunity for eligible first-time buyers
  if (p.firstTimeHomeBuyer && !p.ownsHome) savings += (p.fhsaDeduction >= data.federal.fhsaAnnual ? 15 : p.fhsaDeduction > 0 ? 8 : 0);
  else savings += 12;
  // TFSA signal
  if (p.tfsaRoom != null) savings += p.tfsaRoom > 20000 ? 4 : 12;
  else savings += 10;
  savings = clamp(savings);
  if (rrspUtil < 0.5 && estRoom > 1000) notes.push('Significant unused RRSP room.');

  // 2) Deduction & credit capture ---------------------------------------- 25
  let capture = 55; // baseline: engine already applies the automatic credits
  if (p.donations > 0) capture += 6;
  if (p.medicalExpenses > 0) capture += 6;
  if (p.isStudent && p.tuition > 0) capture += 8; else if (p.isStudent && p.tuition === 0) { capture -= 6; notes.push('Student with no tuition captured.'); }
  if (p.dependants > 0 && p.childCare > 0) capture += 8; else if (p.dependants > 0 && p.childCare === 0) { capture -= 6; notes.push('Dependants but no child-care expenses claimed.'); }
  if (p.employmentIncome > 0 && p.employmentExpenses > 0) capture += 4;
  if (p.unionDues > 0) capture += 3;
  capture = clamp(capture);

  // 3) Tax efficiency ---------------------------------------------------- 15
  // Reward a sensible gap between marginal and average rate (planning headroom used),
  // penalize a large balance owing relative to income.
  let efficiency = 60;
  efficiency += clamp((ret.marginalRate - ret.averageRate) * 1.5, 0, 20);
  if (!ret.isRefund && ret.income.total > 0) {
    const owingRatio = Math.abs(ret.refundOrBalance) / ret.income.total;
    if (owingRatio > 0.08) { efficiency -= 25; notes.push('Large balance owing relative to income.'); }
    else if (owingRatio > 0.02) efficiency -= 12;
  }
  if (rrspUtil >= 0.8) efficiency += 8;
  efficiency = clamp(efficiency);

  // 4) Documentation & compliance ---------------------------------------- 15
  const ds = ret._docSummary || null;
  let documentation;
  if (ds && ds.total > 0) {
    documentation = ds.avgConfidence * 100 * 0.7;
    documentation += (ds.processed / ds.total) * 30;
    if (ds.failed > 0) documentation -= ds.failed * 8;
    if (ds.needsReview > 0) documentation -= ds.needsReview * 4;
  } else {
    documentation = 45; // no documents scanned yet
    notes.push('No documents scanned — audit is based on entered figures only.');
  }
  documentation = clamp(documentation);

  // 5) Planning & carryforwards ------------------------------------------ 15
  let planning = 50;
  if ((p.maritalStatus === 'married' || p.maritalStatus === 'commonlaw')) {
    planning += 8; // household optimization available
    if (p.age && p.age >= 65 && p.pensionIncome > 0) planning += 8; // pension splitting
  }
  if (p.firstTimeHomeBuyer && !p.ownsHome && p.fhsaDeduction > 0) planning += 8;
  if (rrspUtil >= 0.6) planning += 10;
  if (p.tfsaRoom != null && p.tfsaRoom < 10000) planning += 6;
  planning = clamp(planning);

  const subscores = [
    { key: 'savings', label: 'Registered savings', score: pct(savings), weight: 30 },
    { key: 'capture', label: 'Deduction & credit capture', score: pct(capture), weight: 25 },
    { key: 'efficiency', label: 'Tax efficiency', score: pct(efficiency), weight: 15 },
    { key: 'documentation', label: 'Documentation', score: pct(documentation), weight: 15 },
    { key: 'planning', label: 'Planning', score: pct(planning), weight: 15 },
  ];

  const score = Math.round(subscores.reduce((s, c) => s + (c.score * c.weight) / 100, 0));
  const band =
    score >= 85 ? { label: 'Excellent', tone: 'positive' } :
    score >= 70 ? { label: 'Good', tone: 'positive' } :
    score >= 50 ? { label: 'Fair', tone: 'warning' } :
                  { label: 'Needs attention', tone: 'critical' };

  return { score, band, subscores, notes, context: { rrspUtil: Math.round(rrspUtil * 100) / 100, estimatedRrspRoom: Math.round(estRoom) } };
}


/* ===== engine/advisory.js ===== */
/**
 * ONYX Intelligence — advisory / opportunity engine
 * =========================================================================
 * The subject-matter layer: a rules engine encoding the legal tax-reduction
 * strategies a Canadian tax accountant / auditor would look for. Each rule is
 * a self-contained heuristic that:
 *   - decides whether it APPLIES to this filer (profile + computed return),
 *   - estimates a POTENTIAL dollar impact (using the filer's marginal rate),
 *   - explains WHY in plain language, with the relevant statute/CRA reference.
 *
 * Every figure is an ESTIMATE and every recommendation is educational — the
 * language is deliberately "potential/may", never a promise (per professional
 * standards and to avoid overstating tax savings).
 * =========================================================================
 */



const money = (n) => Math.round(n);

/**
 * Build the ordered list of opportunities for a computed return.
 * @param {object} ret  output of computeReturn (with .profile)
 * @param {object} health output of computeTaxHealth
 */
function findOpportunities(ret, health) {
  const p = ret.profile;
  const data = getTaxData(p.year);
  const mr = ret.marginalRate || 0.3; // marginal rate as a decimal
  const out = [];

  const push = (o) => out.push(Object.assign({ impact: null, impactLabel: null, priority: 3 }, o));

  // 1) Unused RRSP room -----------------------------------------------------
  const estRoom = health.context.estimatedRrspRoom;
  const usedRoom = p.rrspDeduction;
  const roomLeft = Math.max(0, estRoom - usedRoom);
  if (roomLeft > 1000 && (p.employmentIncome + p.selfEmploymentIncome) > 0) {
    const suggested = Math.min(roomLeft, Math.max(2000, ret.taxableIncome * 0.1));
    push({
      id: 'rrsp', category: 'Registered savings', priority: 1,
      title: 'Contribute to your RRSP',
      detail: `You appear to have about $${money(roomLeft).toLocaleString('en-CA')} of unused RRSP room. A contribution of roughly $${money(suggested).toLocaleString('en-CA')} could reduce your taxable income and, at your marginal rate, potentially lower your tax by the amount shown.`,
      impact: money(suggested * mr), impactLabel: 'est. tax reduction',
      citation: 'Income Tax Act s.146 · RRSP deduction limit',
    });
  }

  // 2) FHSA for first-time buyers ------------------------------------------
  if (p.firstTimeHomeBuyer && !p.ownsHome) {
    const room = Math.max(0, data.federal.fhsaAnnual - p.fhsaDeduction);
    if (room > 0) push({
      id: 'fhsa', category: 'Registered savings', priority: 1,
      title: 'Open or top up a First Home Savings Account (FHSA)',
      detail: `As a first-time home buyer you can contribute up to $${data.federal.fhsaAnnual.toLocaleString('en-CA')}/year to an FHSA. Contributions are deductible like an RRSP, and qualifying withdrawals for a home are tax-free — a rare combination.`,
      impact: money(room * mr), impactLabel: 'est. tax reduction',
      citation: 'Income Tax Act s.146.6 · FHSA',
    });
  }

  // 3) TFSA sheltering ------------------------------------------------------
  if (p.interestIncome + p.eligibleDividends + p.nonEligibleDividends > 500 && (p.tfsaRoom == null || p.tfsaRoom > 5000)) {
    push({
      id: 'tfsa', category: 'Registered savings', priority: 2,
      title: 'Shelter investment income in a TFSA',
      detail: 'You are reporting taxable investment income. Holding those investments inside a TFSA would let the growth and income compound completely tax-free. Consider using available TFSA room first.',
      impactLabel: 'tax-free growth',
      citation: 'Income Tax Act s.146.2 · TFSA',
    });
  }

  // 4) Tuition — claim / transfer / carry forward --------------------------
  if (p.tuition > 0) {
    push({
      id: 'tuition', category: 'Credits', priority: 2,
      title: 'Use your tuition credit fully',
      detail: `Your $${money(p.tuition).toLocaleString('en-CA')} tuition earns a 15% federal credit plus a provincial amount. If you don't need it all this year, up to $5,000 can be transferred to a parent, grandparent, or spouse, and the rest carries forward indefinitely.`,
      impact: money(p.tuition * 0.15), impactLabel: 'est. federal credit',
      citation: 'Income Tax Act s.118.5 / s.118.9 · Tuition',
    });
  } else if (p.isStudent) {
    push({
      id: 'tuition-missing', category: 'Credits', priority: 2,
      title: 'Add your T2202 tuition certificate',
      detail: 'You indicated you are a student but no tuition has been captured. Download your T2202 from your school\'s portal — the tuition credit is one of the most commonly missed by students.',
      impactLabel: 'missing credit', citation: 'Income Tax Act s.118.5',
    });
  }

  // 5) Medical — pooling & threshold ---------------------------------------
  if (p.medicalExpenses > 0) {
    const married = p.maritalStatus === 'married' || p.maritalStatus === 'commonlaw';
    if (married) push({
      id: 'medical-pool', category: 'Credits', priority: 2,
      title: 'Claim medical expenses on the lower-income spouse',
      detail: 'Medical expenses are reduced by 3% of the claimant\'s net income (up to a cap). Claiming the whole family\'s eligible expenses on the lower-income spouse shrinks that 3% reduction and usually yields a larger credit.',
      impact: money(Math.min(p.medicalExpenses, 2000) * 0.03 * (ret.marginalRate)), impactLabel: 'est. additional credit',
      citation: 'Income Tax Act s.118.2 · Medical expense credit',
    });
    if (ret.credits.medicalEligible <= 0) push({
      id: 'medical-threshold', category: 'Credits', priority: 3,
      title: 'Your medical expenses are below the threshold',
      detail: `Only medical costs above $${money(ret.credits.medicalThreshold).toLocaleString('en-CA')} (3% of net income) count this year. You can claim any 12-month period ending in the tax year — grouping receipts into one window can push you over the threshold.`,
      impactLabel: 'timing', citation: 'Income Tax Act s.118.2',
    });
  }

  // 6) Donations — pooling & carry-forward ---------------------------------
  if (p.donations > 0) {
    push({
      id: 'donations', category: 'Credits', priority: 3,
      title: 'Optimize your charitable donations',
      detail: 'The donation credit jumps from 15% to 29% (federal) on amounts over $200. Pooling both spouses\' donations on one return, or carrying donations forward up to 5 years to cross $200 in a single year, increases the credit.',
      impact: money(Math.max(0, p.donations - 200) * 0.14), impactLabel: 'est. extra credit',
      citation: 'Income Tax Act s.118.1 · Charitable donations',
    });
  }

  // 7) Pension income splitting --------------------------------------------
  if ((p.pensionIncome > 0) && (p.maritalStatus === 'married' || p.maritalStatus === 'commonlaw')) {
    push({
      id: 'pension-split', category: 'Planning', priority: 1,
      title: 'Split eligible pension income with your spouse',
      detail: 'Up to 50% of eligible pension income can be allocated to a lower-income spouse, moving it into a lower tax bracket and potentially preserving age-based credits. This is elected on the return each year.',
      impact: money(Math.min(p.pensionIncome * 0.5, 20000) * (ret.marginalRate * 0.4)), impactLabel: 'est. household saving',
      citation: 'Income Tax Act s.60.03 · Pension income splitting',
    });
  }

  // 8) Canada Workers Benefit ----------------------------------------------
  const workingIncome = p.employmentIncome + p.selfEmploymentIncome;
  if (workingIncome > 3000 && ret.netIncome < data.federal.cwb.familyPhaseOut) {
    push({
      id: 'cwb', category: 'Benefits', priority: 2,
      title: 'You may qualify for the Canada Workers Benefit',
      detail: 'The CWB is a refundable credit for lower-income workers — it pays out even if you owe no tax. Filing a return (and the Schedule 6) is all it takes to receive it.',
      impactLabel: 'refundable benefit', citation: 'Income Tax Act s.122.7 · CWB',
    });
  }

  // 9) Child care expenses --------------------------------------------------
  if (p.dependants > 0 && p.childCare === 0 && workingIncome > 0) {
    push({
      id: 'childcare', category: 'Deductions', priority: 2,
      title: 'Claim your child care expenses',
      detail: 'Eligible child care costs (daycare, day camps, before/after-school care) are deductible so you can work or study — generally claimed by the lower-income spouse. This is a deduction, not just a credit, so it reduces income directly.',
      impactLabel: 'missing deduction', citation: 'Income Tax Act s.63 · Child care expenses',
    });
  }

  // 10) Child benefit --------------------------------------------------------
  if (p.dependants > 0) {
    push({
      id: 'ccb', category: 'Benefits', priority: 3,
      title: 'Make sure you\'re receiving the Canada Child Benefit',
      detail: 'The CCB is a tax-free monthly payment based on family net income and number of children. It is only paid if you (and your spouse) file a return every year.',
      impactLabel: 'tax-free benefit', citation: 'Income Tax Act s.122.6 · CCB',
    });
  }

  // 11) Employment / home-office expenses -----------------------------------
  if (p.employmentIncome > 0 && p.employmentExpenses === 0) {
    push({
      id: 'employment-exp', category: 'Deductions', priority: 3,
      title: 'Check whether you can claim employment expenses',
      detail: 'If your employer requires you to pay for work expenses (home office, supplies, a vehicle) and signs a Form T2200, those costs may be deductible. Many employees who work from home never ask for the form.',
      impactLabel: 'potential deduction', citation: 'Income Tax Act s.8 · Employment expenses / T2200',
    });
  }

  // 12) Spousal amount -------------------------------------------------------
  if ((p.maritalStatus === 'married' || p.maritalStatus === 'commonlaw') && p.spouseNetIncome < data.federal.bpa.max && ret.credits.spousal > 0) {
    push({
      id: 'spousal', category: 'Credits', priority: 3,
      title: 'Spousal amount is available',
      detail: `Because your spouse's net income is below the basic personal amount, you can claim the spousal amount — worth roughly 15% of the shortfall federally, plus a provincial amount. This has been reflected in your estimate.`,
      impact: money(ret.credits.spousal * 0.15), impactLabel: 'est. federal credit',
      citation: 'Income Tax Act s.118(1)(a) · Spousal amount',
    });
  }

  // 13) Capital gains / loss planning ---------------------------------------
  if (p.capitalGains > 0) {
    push({
      id: 'capgains', category: 'Planning', priority: 3,
      title: 'Consider tax-loss harvesting',
      detail: 'Only 50% of a capital gain is taxable, but capital losses can offset gains. Realizing an unrealized loss before year-end (mindful of the 30-day superficial-loss rule) can reduce the tax on this year\'s gains.',
      impactLabel: 'planning', citation: 'Income Tax Act s.38–40 · Capital gains/losses',
    });
  }

  // 14) Balance owing / instalments -----------------------------------------
  if (!ret.isRefund && Math.abs(ret.refundOrBalance) > 3000) {
    push({
      id: 'instalments', category: 'Compliance', priority: 1,
      title: 'Plan for your balance owing',
      detail: `Your estimate shows about $${money(Math.abs(ret.refundOrBalance)).toLocaleString('en-CA')} owing. If this recurs, CRA may require quarterly instalments. Setting money aside now (and considering an RRSP contribution before the deadline) softens the bill.`,
      impactLabel: 'cash-flow', citation: 'Income Tax Act s.156 · Instalments',
    });
  }

  // Sort: quantified impact first (desc) within priority, then qualitative.
  out.sort((a, b) => (a.priority - b.priority) || ((b.impact || 0) - (a.impact || 0)));
  return out;
}

/** "What ONYX found" — a plain-language read of the extracted data. */
function whatWeFound(ret, docSummary) {
  const f = [];
  const i = ret.income;
  if (i.employment > 0) f.push({ ok: true, label: 'Employment income', value: fmt(i.employment) });
  if (i.selfEmployment > 0) f.push({ ok: true, label: 'Self-employment income', value: fmt(i.selfEmployment) });
  if (i.interest > 0 || i.eligibleDividendsGrossed > 0) f.push({ ok: true, label: 'Investment income', value: fmt(i.interest + i.eligibleDividendsGrossed + i.nonEligibleDividendsGrossed) });
  if (ret.taxWithheld > 0) f.push({ ok: true, label: 'Tax already paid', value: fmt(ret.taxWithheld) });
  if (ret.deductions.rrsp > 0) f.push({ ok: true, label: 'RRSP contributions', value: fmt(ret.deductions.rrsp) });
  if (ret.credits.tuition > 0) f.push({ ok: true, label: 'Tuition credit', value: fmt(ret.credits.tuition) });
  if (docSummary && docSummary.needsReview > 0) f.push({ ok: false, label: `${docSummary.needsReview} document(s) need review`, value: 'review' });
  if (docSummary && docSummary.failed > 0) f.push({ ok: false, label: `${docSummary.failed} document(s) could not be read`, value: 'action' });
  return f;
}

const fmt = (n) => '$' + Math.round(n).toLocaleString('en-CA');

/**
 * Compose the full advisory summary (narrative + prioritized actions).
 */
function buildAdvisory(ret, health, opportunities) {
  const p = ret.profile;
  const quantified = opportunities.filter((o) => o.impact).reduce((s, o) => s + o.impact, 0);
  const posLine = ret.isRefund
    ? `Based on the documents provided, you have an estimated refund of ${fmt(ret.refundOrBalance)}.`
    : `Based on the documents provided, you have an estimated balance owing of ${fmt(Math.abs(ret.refundOrBalance))}.`;

  const headline =
    `Your ${ret.provinceName} tax position for ${ret.year} scores ${health.score}/100 (${health.band.label}). ` +
    posLine +
    (quantified > 0 ? ` ONYX has identified about ${fmt(quantified)} in potential tax reductions you may be eligible for.` : '');

  const next = opportunities.slice(0, 3).map((o) => o.title);

  return {
    headline,
    estimatedOpportunity: quantified,
    marginalRate: ret.marginalRate,
    averageRate: ret.averageRate,
    priorityActions: next,
    disclaimer:
      'This is an educational estimate generated from the information provided, not tax advice, an audit-risk assessment, or a filed return. Figures use current federal and provincial parameters but simplify parts of the calculation. Confirm your specifics with the CRA or a licensed tax professional before acting. ONYX does not file your return and is not affiliated with the CRA.',
  };
}


/* ===== engine/checklist.js ===== */
/**
 * ONYX Intelligence — personalized document checklist
 * =========================================================================
 * Compares the taxpayer's situation (profile) against what they've actually
 * provided (extracted figures + uploaded document types) to flag the records
 * ONYX still needs — the "documents to add" an auditor would ask for.
 * =========================================================================
 */

function buildChecklist(ret, documents) {
  const p = ret.profile;
  const docTypes = new Set((documents || []).map((d) => (d.type || '').toUpperCase()));
  const has = (t) => docTypes.has(t);
  const i = ret.income, c = ret.credits, d = ret.deductions;

  const emp = p.employmentType || 'employed';
  const isEmployed = emp === 'employed' || emp === 'mixed';
  const isSelf = emp === 'self-employed' || emp === 'mixed';
  const isRetired = emp === 'retired' || (p.age && p.age >= 65 && i.pension > 0);

  const item = (id, label, applies, provided, required, why) =>
    applies ? { id, label, why, status: provided ? 'provided' : required ? 'missing' : 'suggested' } : null;

  const items = [
    item('t4', 'Employment slips (T4)', isEmployed, i.employment > 0 || has('T4'), true,
      'Your T4 reports employment income and the tax already withheld.'),
    item('self', 'Self-employment records', isSelf, i.selfEmployment > 0 || has('T4A'), true,
      'Business income and expenses — invoices, receipts, and payment-processor reports.'),
    item('pension', 'Pension slips', isRetired, i.pension > 0, isRetired,
      'T4A(P)/OAS and any private pension statements.'),
    item('invest', 'Investment slips (T5 / T3)', p.hasInvestments, i.interest > 0 || i.eligibleDividendsGrossed > 0 || i.nonEligibleDividendsGrossed > 0 || has('T5') || has('T3'), true,
      'Interest, dividends, and trust income for the year.'),
    item('t5008', 'Securities transactions (T5008)', p.hasInvestments, i.taxableCapitalGains > 0 || has('T5008'), false,
      'If you sold investments, ONYX needs the buy/sell records to compute capital gains.'),
    item('crypto', 'Crypto transaction exports', p.hasCrypto, i.taxableCapitalGains > 0 || i.other > 0, true,
      'Exchange statements and wallet history — crypto disposals are taxable.'),
    item('rental', 'Rental income & expense records', p.hasRentalIncome, i.other > 0, true,
      'Rent received plus mortgage interest, property tax, and maintenance.'),
    item('foreign', 'Foreign income & tax paid', p.hasForeignIncome, i.other > 0, true,
      'Foreign slips and currency-conversion records; foreign tax paid may be creditable.'),
    item('rrsp', 'RRSP contribution receipts', i.employment + i.selfEmployment > 0, d.rrsp > 0, false,
      'Contributions lower your taxable income dollar-for-dollar.'),
    item('fhsa', 'FHSA contribution receipts', p.firstTimeHomeBuyer && !p.ownsHome, d.fhsa > 0, false,
      'Deductible like an RRSP, and withdrawals for a first home are tax-free.'),
    item('tuition', 'Tuition certificate (T2202)', p.isStudent, c.tuition > 0, true,
      'One of the most commonly missed credits for students.'),
    item('medical', 'Medical receipts', true, p.medicalExpenses > 0, false,
      'Eligible costs above 3% of net income become a credit.'),
    item('childcare', 'Child-care receipts', p.dependants > 0, p.childCare > 0, false,
      'Deductible so you can work or study — usually claimed by the lower-income spouse.'),
    item('donations', 'Donation receipts', true, p.donations > 0, false,
      'The credit rate jumps from 15% to 29% on amounts over $200.'),
    item('disability', 'Disability certificate (T2201)', p.disability, p.disability, false,
      'The Disability Tax Credit requires a certified T2201 on file with the CRA.'),
    item('carryforward', 'Prior Notice of Assessment', true, p.rrspRoom != null || p.tfsaRoom != null, false,
      'Your NOA confirms RRSP/TFSA room and any carryforward balances — it improves accuracy.'),
    item('spouse', "Spouse's income details", p.maritalStatus === 'married' || p.maritalStatus === 'commonlaw', p.spouseNetIncome != null, false,
      "Needed to optimize spousal credits, pension splitting, and family claims."),
  ].filter(Boolean);

  const provided = items.filter((x) => x.status === 'provided').length;
  const missing = items.filter((x) => x.status === 'missing').length;
  const suggested = items.filter((x) => x.status === 'suggested').length;
  const completeness = items.length ? Math.round((provided / items.length) * 100) : 100;

  return { items, completeness, counts: { provided, missing, suggested, applicable: items.length } };
}


/* ===== engine/planner.js ===== */
/**
 * ONYX Intelligence — planning tools (QOL)
 *   - optimizeRRSP     : solve the RRSP contribution to erase owing / drop a bracket
 *   - accountPriority  : which registered account to prioritize (RRSP/TFSA/FHSA)
 *   - estimateBenefits : rough estimates of refundable government benefits
 *   - taxCalendar      : the taxpayer's next key CRA deadlines, with countdowns
 * Estimates are clearly labelled; benefit formulas are simplified.
 */


const r0 = (n) => Math.round(n);
const ceilTo = (n, step) => Math.ceil(n / step) * step;
const pctLabel = (rate) => (Math.round(rate * 1000) / 10) + '%';
function rateAtIncome(income, brackets) {
  for (const b of brackets) if (income <= b.upTo) return b.rate;
  return brackets[brackets.length - 1].rate;
}

/** Solve useful RRSP contributions using the real engine (year-aware). */
function optimizeRRSP(ret, estRoom) {
  const base = ret.profile;
  const brackets = getTaxData(base.year).federal.brackets;
  const roomLeft = Math.max(0, (estRoom || 0) - base.rrspDeduction);
  const moves = [];
  if (roomLeft < 500 || (base.employmentIncome + base.selfEmploymentIncome) <= 0) return moves;

  const recompute = (extra) =>
    computeReturn(Object.assign({}, base, { rrspDeduction: base.rrspDeduction + Math.min(extra, roomLeft) }));

  // 1) Erase a balance owing.
  if (!ret.isRefund && ret.refundOrBalance < -1) {
    const guess = Math.min(roomLeft, ceilTo(Math.abs(ret.refundOrBalance) / Math.max(ret.marginalRate, 0.15), 100));
    const after = recompute(guess);
    moves.push({
      type: 'erase', contribution: r0(Math.min(guess, roomLeft)), newRefund: after.refundOrBalance,
      label: `Contribute about $${r0(Math.min(guess, roomLeft)).toLocaleString('en-CA')} to wipe out your balance owing`,
      note: 'Brings your estimated balance close to zero at your marginal rate.',
    });
  }

  // 2) Drop into the next-lower federal bracket.
  const thresholds = brackets.slice(0, -1).map((b) => b.upTo);
  const ti = ret.taxableIncome;
  const th = thresholds.filter((t) => t < ti - 1).pop();
  if (th) {
    const need = ceilTo(ti - th, 100);
    if (need > 0 && need <= roomLeft) {
      const after = recompute(need);
      moves.push({
        type: 'bracket', contribution: r0(need), newRefund: after.refundOrBalance,
        label: `Contribute $${r0(need).toLocaleString('en-CA')} to drop from the ${pctLabel(rateAtIncome(ti, brackets))} bracket into the ${pctLabel(rateAtIncome(th - 1, brackets))} bracket`,
        note: 'Lowers the tax rate on your top dollars of income.',
      });
    }
  }

  return moves.map((m) => Object.assign(m, { delta: r0(m.newRefund - ret.refundOrBalance), roomLeft: r0(roomLeft) }));
}

/** Which registered account to prioritize, given the situation. */
function accountPriority(ret) {
  const p = ret.profile;
  const mr = ret.marginalRate;
  const tips = [];
  if (p.firstTimeHomeBuyer && !p.ownsHome) tips.push({ account: 'FHSA', why: 'Saving toward a first home: FHSA is deductible now and tax-free out — the best of both.' });
  if (mr >= 0.3) tips.push({ account: 'RRSP', why: `At a ${pctLabel(mr)} marginal rate, RRSP deductions give a large up-front refund.` });
  tips.push({ account: 'TFSA', why: mr < 0.3 ? 'At a lower marginal rate, TFSA (tax-free growth) often beats an RRSP deduction.' : 'Use TFSA room for tax-free growth once RRSP/FHSA are handled.' });
  const order = [...new Set(tips.map((t) => t.account))];
  return { order, tips: tips.slice(0, 3) };
}

/** Rough estimates of refundable government benefits (paid separately from a refund). */
function estimateBenefits(ret) {
  const p = ret.profile;
  const fed = getTaxData(p.year).federal;
  const net = ret.netIncome;
  const kids = p.dependants || 0;
  const married = p.maritalStatus === 'married' || p.maritalStatus === 'commonlaw';
  const familyNet = net + (married ? Math.max(0, p.spouseNetIncome) : 0);
  const out = [];

  // GST/HST credit (approximate; reduced by 5% of family net income over ~$45k)
  const g = fed.gstCredit;
  const gstBase = g.single + (married ? g.base - g.single : 0) + kids * g.perChild;
  const gst = Math.max(0, gstBase - 0.05 * Math.max(0, familyNet - 45000));
  if (gst > 20) out.push({ id: 'gst', label: 'GST/HST credit', amount: r0(gst), note: 'Quarterly, tax-free — for lower-income individuals and families.' });

  // Canada Carbon Rebate (fuel-charge provinces only; flat, not income-tested)
  const CCR = { AB: 900, SK: 752, MB: 600, ON: 560, NB: 380, NS: 412, PE: 440, NL: 596 };
  if (CCR[p.province]) {
    const b = CCR[p.province];
    out.push({ id: 'ccr', label: 'Canada Carbon Rebate', amount: r0(b + (married ? b * 0.5 : 0) + kids * b * 0.25), note: 'Quarterly, tax-free — automatic when you file in an eligible province.' });
  }

  // Canada Child Benefit (approximate; young-child rate, phased on family net income)
  if (kids > 0) {
    const maxPer = 7787; // under-6 max; a conservative upper bound
    let ccb = kids * maxPer;
    const rate = kids === 1 ? 0.07 : kids === 2 ? 0.135 : kids === 3 ? 0.19 : 0.23;
    ccb = Math.max(0, ccb - rate * Math.max(0, familyNet - 36502));
    if (ccb > 50) out.push({ id: 'ccb', label: 'Canada Child Benefit', amount: r0(ccb), note: 'Monthly, tax-free — based on family net income and number/age of children.' });
  }

  return { items: out, total: r0(out.reduce((s, x) => s + x.amount, 0)) };
}

/** The taxpayer's next key CRA deadlines from today, with countdowns. */
function taxCalendar(profile, now) {
  const today = now ? new Date(now) : new Date();
  today.setHours(0, 0, 0, 0);
  const emp = (profile && profile.employmentType) || 'employed';
  const isSelf = emp === 'self-employed' || emp === 'mixed';
  const nextOccur = (month, day) => {
    let d = new Date(today.getFullYear(), month - 1, day);
    if (d < today) d = new Date(today.getFullYear() + 1, month - 1, day);
    return d;
  };
  const rows = [
    { m: 1, d: 1, label: 'New TFSA, FHSA & RRSP room', tag: 'planning', note: 'A fresh year of contribution room opens.' },
    { m: 3, d: 1, label: 'RRSP contribution deadline', tag: 'rrsp', note: 'Last day to contribute for the prior tax year.' },
    { m: 4, d: 30, label: 'Filing deadline & balance due', tag: 'filing', note: 'Return due for most individuals; any balance owing is due today.' },
    { m: 12, d: 31, label: 'Year-end tax moves & donations', tag: 'planning', note: 'Last day for donations and most in-year tax moves.' },
  ];
  if (isSelf) {
    rows.push({ m: 6, d: 15, label: 'Self-employed filing deadline', tag: 'filing', note: 'Return due if you (or your spouse) had self-employment income.' });
    [[3, 15], [6, 15], [9, 15], [12, 15]].forEach(([m, d]) => rows.push({ m, d, label: 'Quarterly instalment', tag: 'instalment', note: 'CRA instalment payment, if required.' }));
  }
  const oneDay = 86400000;
  return rows
    .map((r) => { const date = nextOccur(r.m, r.d); return { label: r.label, tag: r.tag, note: r.note, date: date.toISOString().slice(0, 10), daysAway: Math.round((date - today) / oneDay) }; })
    .sort((a, b) => a.daysAway - b.daysAway)
    .slice(0, 6);
}


/* ===== engine/index.js ===== */
/**
 * ONYX Intelligence — engine entry point
 * Orchestrates: scan documents → compute return → score health →
 * find opportunities → build advisory. One call: runAudit().
 */



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
  const ov = (v) => Math.max(0, Math.min(1e7, +v || 0)); // clamp what-if inputs to a sane range
  input.rrspDeduction = (input.rrspDeduction || 0) + ov(overrides.rrsp);
  input.fhsaDeduction = (input.fhsaDeduction || 0) + ov(overrides.fhsa);
  input.donations = (input.donations || 0) + ov(overrides.donations);
  input.employmentIncome = (input.employmentIncome || 0) + ov(overrides.extraIncome);
  input.capitalGains = (input.capitalGains || 0) + ov(overrides.capitalGains);
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


/* ===== Local (serverless) client — implements the same ONYX.api interface
   the pages use, but computes everything in-browser with the engine above and
   persists to localStorage. NOTE: client-side accounts are for local/demo use
   only — passwords are obfuscated, not securely hashed. For real multi-user
   auth, deploy the Node API (server/) instead. ===== */

var slipTypes = {};
Object.keys(SLIP_MAP).forEach(function (k) { slipTypes[k] = SLIP_MAP[k].label; });

var LS = {
  get: function (k, d) { try { var v = JSON.parse(localStorage.getItem(k)); return v == null ? d : v; } catch (_) { return d; } },
  set: function (k, v) { localStorage.setItem(k, JSON.stringify(v)); },
};
function usersDB() { return LS.get('onyx_users', {}); }
function saveUsers(u) { LS.set('onyx_users', u); }
function emailIndex() { return LS.get('onyx_email', {}); }
function newId() { return 'u_' + Math.random().toString(36).slice(2) + Date.now().toString(36); }
function publicUser(u) { return { id: u.id, email: u.email, name: u.name, createdAt: u.createdAt }; }
function currentUser() { var t = localStorage.getItem('onyx_token'); if (!t) return null; return usersDB()[t] || null; }

function localApi(path, opts) {
  opts = opts || {};
  var method = opts.method || 'GET';
  var body = opts.body || {};

  if (path === '/meta') return { provinces: PROVINCE_NAMES, slipTypes: slipTypes, years: (typeof AVAILABLE_YEARS !== 'undefined' ? AVAILABLE_YEARS : [2024]), year: 2024 };

  if (path === '/auth/register') {
    var email = (body.email || '').toLowerCase().trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw new Error('Enter a valid email address.');
    if (!body.password || body.password.length < 8) throw new Error('Password must be at least 8 characters.');
    var users = usersDB(), idx = emailIndex();
    if (idx[email]) throw new Error('An account with that email already exists.');
    var u = { id: newId(), email: email, name: body.name || email.split('@')[0], pwd: btoa(body.password),
      createdAt: new Date().toISOString(), profile: { province: 'ON', year: 2024, maritalStatus: 'single' }, documents: [], audit: null };
    users[u.id] = u; idx[email] = u.id; saveUsers(users); LS.set('onyx_email', idx);
    return { token: u.id, user: publicUser(u) };
  }
  if (path === '/auth/login') {
    var em = (body.email || '').toLowerCase().trim(), ix = emailIndex(), us = usersDB();
    var user = ix[em] ? us[ix[em]] : null;
    if (!user || user.pwd !== btoa(body.password || '')) throw new Error('Incorrect email or password.');
    return { token: user.id, user: publicUser(user) };
  }

  var me = currentUser();
  if (!me) throw new Error('Not signed in.');
  var save = function () { var u = usersDB(); u[me.id] = me; saveUsers(u); };

  if (path === '/me') return { user: publicUser(me) };
  if (path === '/profile' && method === 'GET') return { profile: me.profile };
  if (path === '/profile' && method === 'PUT') {
    ['province','year','age','maritalStatus','spouseNetIncome','dependants','isStudent','disability','firstTimeHomeBuyer','ownsHome','rrspRoom','tfsaRoom','employmentType','hasInvestments','hasRentalIncome','hasForeignIncome','hasCrypto']
      .forEach(function (f) { if (f in body) me.profile[f] = body[f]; });
    save(); return { profile: me.profile };
  }
  if (path === '/documents' && method === 'GET') return { documents: me.documents };
  if (path === '/documents' && method === 'POST') {
    var type = (body.type || '').toUpperCase();
    if (!SLIP_MAP[type]) throw new Error('Unknown document type.');
    if (!body.fields && !body.text) throw new Error('Provide either extracted fields or document text.');
    var scan = scanDocuments([{ type: type, fields: body.fields, text: body.text, name: body.name }]);
    var rec = { id: newId(), uploadedAt: new Date().toISOString(), type: type, fields: body.fields, text: body.text, name: body.name, scan: scan.documents[0] };
    me.documents.push(rec); save(); return { document: rec };
  }
  if (path.indexOf('/documents/') === 0 && method === 'DELETE') {
    var did = path.split('/')[2], n = me.documents.length;
    me.documents = me.documents.filter(function (d) { return d.id !== did; }); save();
    return { ok: me.documents.length < n };
  }
  if (path === '/audit' && method === 'POST') {
    var docs = me.documents.map(function (d) { return { type: d.type, fields: d.fields, text: d.text, name: d.name }; });
    var audit = runAudit({ profile: me.profile, documents: docs, year: me.profile.year });
    me.audit = audit; save(); return { audit: audit };
  }
  if (path === '/audit' && method === 'GET') return { audit: me.audit || null };

  if (path === '/simulate' && method === 'POST') {
    var docs = me.documents.map(function (d) { return { type: d.type, fields: d.fields, text: d.text }; });
    var result = simulate({ profile: me.profile, documents: docs, year: me.profile.year, overrides: (body && body.overrides) || {} });
    return { result: result };
  }
  if (path === '/export' && method === 'GET') {
    return { exportedAt: new Date().toISOString(), account: publicUser(me), profile: me.profile, documents: me.documents, audit: me.audit };
  }
  if (path === '/account' && method === 'DELETE') {
    var users = usersDB(), idx = emailIndex();
    delete users[me.id]; delete idx[me.email];
    saveUsers(users); LS.set('onyx_email', idx);
    return { ok: true };
  }

  throw new Error('Unknown route: ' + path);
}

window.ONYX = {
  token: function () { return localStorage.getItem('onyx_token'); },
  user: function () { try { return JSON.parse(localStorage.getItem('onyx_user')); } catch (_) { return null; } },
  setAuth: function (t, u) { localStorage.setItem('onyx_token', t); localStorage.setItem('onyx_user', JSON.stringify(u)); },
  signOut: function () { localStorage.removeItem('onyx_token'); localStorage.removeItem('onyx_user'); location.href = 'login.html'; },
  api: function (path, opts) {
    return new Promise(function (resolve, reject) {
      try { resolve(localApi(path, opts || {})); } catch (e) { reject(e); }
    });
  },
  requireAuth: function () { if (!this.token()) location.href = 'login.html'; },
  money: function (n) { return (n < 0 ? '-$' : '$') + Math.abs(Math.round(n)).toLocaleString('en-CA'); },
  pctStr: function (n) { return (n * 100).toFixed(1) + '%'; },
};

})();
