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
 * 2025 tax year.
 * FEDERAL — verified against CRA "Tax rates and income brackets" (2025).
 *   The lowest federal rate was cut from 15% to 14% effective July 1, 2025,
 *   giving a BLENDED 14.5% rate for the 2025 tax year. Non-refundable credits
 *   (incl. the basic personal amount) and the first $200 of donations are
 *   therefore valued at 14.5% for 2025. Source: canada.ca — CRA tax rates.
 * PROVINCIAL — the 2024 tables indexed forward (~2.8%) as a PRELIMINARY estimate,
 *   with Alberta's new 2025 8% bracket applied explicitly. Verify against each
 *   province's published 2025 figures before relying on provincial amounts.
 */
const FEDERAL_2025 = {
  brackets: [
    { upTo: 57375, rate: 0.145 }, { upTo: 114750, rate: 0.205 }, { upTo: 177882, rate: 0.26 },
    { upTo: 253414, rate: 0.29 }, { upTo: Infinity, rate: 0.33 },
  ],
  bpa: { max: 16129, min: 14538, phaseStart: 177882, phaseEnd: 253414 },
  creditRate: 0.145, // 2025 non-refundable credits valued at the blended 14.5% rate
  canadaEmployment: 1471,
  pensionIncomeMax: 2000,
  ageAmount: { max: 9028, threshold: 45522, rate: 0.15 },
  cpp: { maxPensionable: 71300, exemption: 3500, rate: 0.0595, max: 4034.1, cpp2: { lower: 71300, upper: 81200, rate: 0.04, max: 396 } },
  ei: { maxInsurable: 65700, rate: 0.0164, max: 1077.48 },
  medical: { pct: 0.03, cap: 2834 },
  donation: { threshold: 200, low: 0.145, high: 0.29, top: 0.33, topBracket: 253414 },
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
// By default, indexed 2025 figures are preliminary estimates until confirmed
// against each jurisdiction's published amounts.
for (const code of Object.keys(PROVINCES_2025)) PROVINCES_2025[code].verified2025 = false;

// ---------------------------------------------------------------------------
// 2025 provincial figures VERIFIED against official sources (ON, BC, AB, QC —
// the four most populous jurisdictions). Each carries verified2025: true.
// ---------------------------------------------------------------------------

// Ontario 2025 — verified vs CRA provincial rates. Note the top two bracket
// thresholds ($150,000 / $220,000) are NOT indexed; only the first two are.
PROVINCES_2025.ON = {
  name: 'Ontario',
  brackets: [
    { upTo: 52886, rate: 0.0505 }, { upTo: 105775, rate: 0.0915 }, { upTo: 150000, rate: 0.1116 },
    { upTo: 220000, rate: 0.1216 }, { upTo: Infinity, rate: 0.1316 },
  ],
  bpa: 12747, creditRate: 0.0505,
  surtax: [ { over: 5710, rate: 0.20 }, { over: 7307, rate: 0.36 } ],
  healthPremium: PROVINCES_2024.ON.healthPremium,
  verified2025: true,
};

// British Columbia 2025 — verified vs CRA provincial rates.
PROVINCES_2025.BC = {
  name: 'British Columbia',
  brackets: [
    { upTo: 49279, rate: 0.0506 }, { upTo: 98560, rate: 0.077 }, { upTo: 113158, rate: 0.105 },
    { upTo: 137407, rate: 0.1229 }, { upTo: 186306, rate: 0.147 }, { upTo: 259829, rate: 0.168 },
    { upTo: Infinity, rate: 0.205 },
  ],
  bpa: 12932, creditRate: 0.0506,
  verified2025: true,
};

// Alberta 2025 — verified. New 8% bracket on the first $60,000 (credits at 10%).
PROVINCES_2025.AB = {
  name: 'Alberta',
  brackets: [
    { upTo: 60000, rate: 0.08 }, { upTo: 151234, rate: 0.10 }, { upTo: 181481, rate: 0.12 },
    { upTo: 241974, rate: 0.13 }, { upTo: 362961, rate: 0.14 }, { upTo: Infinity, rate: 0.15 },
  ],
  bpa: 22323, creditRate: 0.10,
  verified2025: true,
};

// Quebec 2025 — verified vs Revenu Québec published brackets (credits at 14%).
PROVINCES_2025.QC = {
  name: 'Quebec',
  brackets: [
    { upTo: 53255, rate: 0.14 }, { upTo: 106495, rate: 0.19 }, { upTo: 129590, rate: 0.24 },
    { upTo: Infinity, rate: 0.2575 },
  ],
  bpa: 18571, creditRate: 0.14,
  abatement: 0.165,
  verified2025: true,
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

  // Every opportunity answers four questions for the user:
  //   category   — the kind of move (drives grouping in the UI)
  //   mechanism  — a short tag for HOW it saves tax (what actually happens)
  //   where      — WHERE to put the money / which account, line, or person
  //   how        — the concrete step to take (with amounts/deadlines)
  //   why        — WHY it lowers your tax, in plain language
  const cash = (n) => '$' + money(n).toLocaleString('en-CA');
  const push = (o) => out.push(Object.assign({ impact: null, impactLabel: null, priority: 3, mechanism: '' }, o));

  // 1) Unused RRSP room -----------------------------------------------------
  const estRoom = health.context.estimatedRrspRoom;
  const usedRoom = p.rrspDeduction;
  const roomLeft = Math.max(0, estRoom - usedRoom);
  if (roomLeft > 1000 && (p.employmentIncome + p.selfEmploymentIncome) > 0) {
    const suggested = Math.min(roomLeft, Math.max(2000, ret.taxableIncome * 0.1));
    push({
      id: 'rrsp', category: 'Registered accounts', priority: 1,
      title: 'Contribute to your RRSP',
      mechanism: 'Lowers your taxable income',
      where: 'Your RRSP (Registered Retirement Savings Plan)',
      how: `Contribute about ${cash(suggested)} before the deadline (the first 60 days of the year). You appear to have roughly ${cash(roomLeft)} of unused room.`,
      why: `Every dollar you put in is subtracted from the income you're taxed on, so at your ${Math.round(mr * 100)}% marginal rate it comes straight off your tax bill. The money then grows sheltered until you withdraw it in retirement, usually at a lower rate.`,
      impact: money(suggested * mr), impactLabel: 'est. tax reduction',
      citation: 'Income Tax Act s.146 · RRSP deduction limit',
    });
  }

  // 2) FHSA for first-time buyers ------------------------------------------
  if (p.firstTimeHomeBuyer && !p.ownsHome) {
    const room = Math.max(0, data.federal.fhsaAnnual - p.fhsaDeduction);
    if (room > 0) push({
      id: 'fhsa', category: 'Registered accounts', priority: 1,
      title: 'Open or top up a First Home Savings Account (FHSA)',
      mechanism: 'Deductible going in, tax-free coming out',
      where: 'A First Home Savings Account (FHSA)',
      how: `Open an FHSA and contribute up to ${cash(room)} this year (annual limit ${cash(data.federal.fhsaAnnual)}).`,
      why: 'It is the best of both worlds: contributions are deductible like an RRSP — lowering your taxable income now — and qualifying withdrawals to buy your first home come out completely tax-free.',
      impact: money(room * mr), impactLabel: 'est. tax reduction',
      citation: 'Income Tax Act s.146.6 · FHSA',
    });
  }

  // 3) TFSA sheltering ------------------------------------------------------
  if (p.interestIncome + p.eligibleDividends + p.nonEligibleDividends > 500 && (p.tfsaRoom == null || p.tfsaRoom > 5000)) {
    push({
      id: 'tfsa', category: 'Registered accounts', priority: 2,
      title: 'Shelter investment income in a TFSA',
      mechanism: 'Tax-free growth, forever',
      where: 'A Tax-Free Savings Account (TFSA)',
      how: 'Move your interest- and dividend-paying investments inside your TFSA, using available room first.',
      why: 'You are paying tax on this investment income every single year. Held inside a TFSA, the same growth and income are 100% tax-free — permanently.',
      impactLabel: 'tax-free growth',
      citation: 'Income Tax Act s.146.2 · TFSA',
    });
  }

  // 4) Tuition — claim / transfer / carry forward --------------------------
  if (p.tuition > 0) {
    push({
      id: 'tuition', category: 'Tax credits', priority: 2,
      title: 'Use your tuition credit fully',
      mechanism: 'Cuts your tax directly',
      where: 'Your return — or transfer up to $5,000 to a parent, grandparent, or spouse',
      how: `Claim your ${cash(p.tuition)} of tuition. If you don't need it all this year, transfer up to $5,000 to a supporting family member and carry the remainder forward.`,
      why: 'Tuition is a credit that reduces tax dollar-for-dollar at ~15% federally plus a provincial amount. Unused tuition is never lost — it transfers or carries forward indefinitely.',
      impact: money(p.tuition * 0.15), impactLabel: 'est. federal credit',
      citation: 'Income Tax Act s.118.5 / s.118.9 · Tuition',
    });
  } else if (p.isStudent) {
    push({
      id: 'tuition-missing', category: 'Tax credits', priority: 2,
      title: 'Add your T2202 tuition certificate',
      mechanism: 'Cuts your tax directly',
      where: "Your school's student portal (the T2202 slip)",
      how: 'Download your T2202 from your school and add it to your documents so the credit is captured.',
      why: 'You indicated you are a student but no tuition was found. The tuition credit is one of the most commonly missed by students — it lowers your tax directly, and any unused part carries forward.',
      impactLabel: 'missing credit', citation: 'Income Tax Act s.118.5',
    });
  }

  // 5) Medical — pooling & threshold ---------------------------------------
  if (p.medicalExpenses > 0) {
    const married = p.maritalStatus === 'married' || p.maritalStatus === 'commonlaw';
    if (married) push({
      id: 'medical-pool', category: 'Tax credits', priority: 2,
      title: 'Claim medical expenses on the lower-income spouse',
      mechanism: 'Cuts your tax directly',
      where: "The lower-income spouse's return",
      how: "Total the whole family's eligible medical receipts and claim them on whichever spouse has the lower net income.",
      why: 'Medical expenses only count above 3% of the claimant\'s net income. Putting them on the lower earner shrinks that 3% floor, so more of your receipts convert into an actual credit.',
      impact: money(Math.min(p.medicalExpenses, 2000) * 0.03 * (ret.marginalRate)), impactLabel: 'est. additional credit',
      citation: 'Income Tax Act s.118.2 · Medical expense credit',
    });
    if (ret.credits.medicalEligible <= 0) push({
      id: 'medical-threshold', category: 'Tax credits', priority: 3,
      title: 'Your medical expenses are below the threshold',
      mechanism: 'Timing move',
      where: 'A single 12-month claim window',
      how: `Group receipts into one 12-month period ending in the tax year to push past the ${cash(ret.credits.medicalThreshold)} threshold.`,
      why: 'Only medical costs above 3% of your net income count this year. Bunching two years of expenses into one claim period can lift you over that line so they finally count.',
      impactLabel: 'timing', citation: 'Income Tax Act s.118.2',
    });
  }

  // 6) Donations — pooling & carry-forward ---------------------------------
  if (p.donations > 0) {
    push({
      id: 'donations', category: 'Tax credits', priority: 3,
      title: 'Optimize your charitable donations',
      mechanism: 'Cuts your tax directly',
      where: 'One spouse\'s return (pooled), or carried forward up to 5 years',
      how: 'Combine both spouses\' donation receipts on a single return, or save smaller donations and claim them together once they exceed $200.',
      why: 'The federal credit jumps from 15% to 29% on the portion above $200 — so pooling everything into one larger claim is worth noticeably more than scattering it.',
      impact: money(Math.max(0, p.donations - 200) * 0.14), impactLabel: 'est. extra credit',
      citation: 'Income Tax Act s.118.1 · Charitable donations',
    });
  }

  // 7) Pension income splitting --------------------------------------------
  if ((p.pensionIncome > 0) && (p.maritalStatus === 'married' || p.maritalStatus === 'commonlaw')) {
    push({
      id: 'pension-split', category: 'Income splitting', priority: 1,
      title: 'Split eligible pension income with your spouse',
      mechanism: 'Moves income to a lower tax bracket',
      where: "Your spouse's return (up to 50% of eligible pension income)",
      how: 'Elect on your return each year to move up to half of your eligible pension income onto your lower-income spouse.',
      why: 'It shifts income out of your higher bracket into your spouse\'s lower one, trims the household tax bill, and can help preserve age-based credits.',
      impact: money(Math.min(p.pensionIncome * 0.5, 20000) * (ret.marginalRate * 0.4)), impactLabel: 'est. household saving',
      citation: 'Income Tax Act s.60.03 · Pension income splitting',
    });
  }

  // 8) Canada Workers Benefit ----------------------------------------------
  const workingIncome = p.employmentIncome + p.selfEmploymentIncome;
  if (workingIncome > 3000 && ret.netIncome < data.federal.cwb.familyPhaseOut) {
    push({
      id: 'cwb', category: 'Government benefits', priority: 2,
      title: 'You may qualify for the Canada Workers Benefit',
      mechanism: 'Refundable — pays out even at $0 tax',
      where: 'Filed on Schedule 6 of your return',
      how: 'File your return with Schedule 6 — even if you owe no tax at all.',
      why: 'The CWB is a refundable credit for lower-income workers: the government pays it to you even when your tax is zero. Filing is all it takes to receive it.',
      impactLabel: 'refundable benefit', citation: 'Income Tax Act s.122.7 · CWB',
    });
  }

  // 9) Child care expenses --------------------------------------------------
  if (p.dependants > 0 && p.childCare === 0 && workingIncome > 0) {
    push({
      id: 'childcare', category: 'Deductions', priority: 2,
      title: 'Claim your child care expenses',
      mechanism: 'Lowers your taxable income',
      where: "The lower-income spouse's return (line 21400)",
      how: 'Gather daycare, day-camp and before/after-school receipts and claim them, generally on the lower-earning spouse.',
      why: 'Child care is a deduction, not just a credit — it comes straight off your income before tax is calculated, so it is worth your full marginal rate.',
      impactLabel: 'missing deduction', citation: 'Income Tax Act s.63 · Child care expenses',
    });
  }

  // 10) Child benefit --------------------------------------------------------
  if (p.dependants > 0) {
    push({
      id: 'ccb', category: 'Government benefits', priority: 3,
      title: "Make sure you're receiving the Canada Child Benefit",
      mechanism: 'Tax-free monthly payment',
      where: 'Paid monthly by the CRA once you file',
      how: 'Make sure both you and your spouse file a return every year to keep the payments flowing.',
      why: 'The CCB is a tax-free monthly payment based on family net income and number of children — but the CRA only pays it if your returns are filed.',
      impactLabel: 'tax-free benefit', citation: 'Income Tax Act s.122.6 · CCB',
    });
  }

  // 11) Employment / home-office expenses -----------------------------------
  if (p.employmentIncome > 0 && p.employmentExpenses === 0) {
    push({
      id: 'employment-exp', category: 'Deductions', priority: 3,
      title: 'Check whether you can claim employment expenses',
      mechanism: 'Lowers your taxable income',
      where: 'Your return (line 22900), backed by a signed Form T2200',
      how: 'Ask your employer to sign Form T2200, then claim eligible home-office, supplies or vehicle costs.',
      why: 'If your job requires you to pay for these, they are deductible — reducing your taxable income at your full marginal rate. Many work-from-home employees simply never ask for the form.',
      impactLabel: 'potential deduction', citation: 'Income Tax Act s.8 · Employment expenses / T2200',
    });
  }

  // 12) Spousal amount -------------------------------------------------------
  if ((p.maritalStatus === 'married' || p.maritalStatus === 'commonlaw') && p.spouseNetIncome < data.federal.bpa.max && ret.credits.spousal > 0) {
    push({
      id: 'spousal', category: 'Tax credits', priority: 3,
      title: 'Spousal amount is available',
      mechanism: 'Cuts your tax directly',
      where: 'Your return (the spousal amount)',
      how: 'Already applied in your estimate — claim the spousal amount because your spouse\'s income is below the basic personal amount.',
      why: 'It gives you a credit worth about 15% federally (plus a provincial amount) of the gap between your spouse\'s income and the basic personal amount.',
      impact: money(ret.credits.spousal * 0.15), impactLabel: 'est. federal credit',
      citation: 'Income Tax Act s.118(1)(a) · Spousal amount',
    });
  }

  // 13) Capital gains / loss planning ---------------------------------------
  if (p.capitalGains > 0) {
    push({
      id: 'capgains', category: 'Income splitting', priority: 3,
      title: 'Consider tax-loss harvesting',
      mechanism: 'Offsets taxable gains',
      where: 'Your non-registered (taxable) investment account',
      how: 'Before year-end, consider selling an investment that is down to realize the loss — then wait 30 days before rebuying it (the superficial-loss rule).',
      why: 'Only 50% of a capital gain is taxable, and capital losses cancel out capital gains. Harvesting a loss trims the tax on this year\'s taxable gains.',
      impactLabel: 'planning', citation: 'Income Tax Act s.38–40 · Capital gains/losses',
    });
  }

  // 14) Balance owing / instalments -----------------------------------------
  if (!ret.isRefund && Math.abs(ret.refundOrBalance) > 3000) {
    push({
      id: 'instalments', category: 'Cash-flow & compliance', priority: 1,
      title: 'Plan for your balance owing',
      mechanism: 'Avoids interest & instalment surprises',
      where: 'A separate savings buffer (and possibly CRA instalments)',
      how: `Set money aside for the ~${cash(Math.abs(ret.refundOrBalance))} owing, and consider an RRSP contribution before the deadline to shrink it.`,
      why: 'A recurring balance owing can trigger mandatory quarterly instalments. Planning ahead avoids CRA interest and a cash-flow crunch at filing time.',
      impactLabel: 'cash-flow', citation: 'Income Tax Act s.156 · Instalments',
    });
  }

  // ---- Coach-style strategies (established Canadian tax-saving moves) ----
  const married = p.maritalStatus === 'married' || p.maritalStatus === 'commonlaw';
  const incomeGap = ret.netIncome - (married ? p.spouseNetIncome : ret.netIncome);
  const hasInvestments = p.hasInvestments || (p.interestIncome + p.eligibleDividends + p.nonEligibleDividends) > 0 || p.capitalGains > 0;

  // 15) Spousal RRSP — split income into retirement --------------------------
  if (married && incomeGap > 15000 && (p.employmentIncome + p.selfEmploymentIncome) > 0) {
    push({
      id: 'spousal-rrsp', category: 'Income splitting', priority: 1,
      title: 'Use a spousal RRSP to split future income',
      mechanism: 'Deduct now high, withdraw later low',
      where: 'A spousal RRSP (you contribute, your spouse owns it)',
      how: 'Contribute to a spousal RRSP using your own RRSP room — it stacks on top of your regular RRSP contributions.',
      why: 'You take the deduction now at your higher rate; the money is later withdrawn and taxed in your spouse\'s hands at their lower rate. Net result: less household tax over time.',
      impact: money(Math.min(roomLeft || 6000, 6000) * Math.max(mr - 0.2, 0.05)), impactLabel: 'est. household saving',
      citation: 'Income Tax Act s.146(5.1) · Spousal RRSP',
    });
  }

  // 16) Home Buyers' Plan — stack with FHSA ----------------------------------
  if (p.firstTimeHomeBuyer && !p.ownsHome) {
    push({
      id: 'hbp', category: 'Registered accounts', priority: 2,
      title: 'Stack the Home Buyers’ Plan with your FHSA',
      mechanism: 'Tax-free withdrawal for a home',
      where: 'Your RRSP (a Home Buyers’ Plan withdrawal)',
      how: 'As a first-time buyer, withdraw up to $60,000 from your RRSP tax-free for a down payment (repaid over 15 years) — stacked on top of your FHSA.',
      why: 'You still get the deduction when you contribute, then pull the money out tax-free for the home. Combined with an FHSA it builds a much larger tax-advantaged down payment.',
      impactLabel: 'tax-free withdrawal', citation: 'Income Tax Act s.146.01 · Home Buyers’ Plan',
    });
  }

  // 17) Donate appreciated securities in-kind --------------------------------
  if (hasInvestments && (p.donations > 0 || p.capitalGains > 0)) {
    const avoided = p.capitalGains > 0 ? money(p.capitalGains * data.federal.capitalGainsInclusion * mr) : null;
    push({
      id: 'donate-securities', category: 'Tax credits', priority: 2,
      title: 'Donate appreciated stock instead of cash',
      mechanism: 'Erases the gains tax + full credit',
      where: 'Directly to the charity — in shares, not cash',
      how: 'Transfer appreciated publicly-traded stock or ETFs straight to a registered charity, rather than selling them and donating the cash.',
      why: 'Donating shares in-kind eliminates the capital-gains tax on them entirely, and you still receive the full donation credit on their market value — the single most tax-efficient way to give.',
      impact: avoided, impactLabel: avoided ? 'est. gains tax avoided' : 'gains-tax-free giving',
      citation: 'Income Tax Act s.38(a.1) · Gifts of listed securities',
    });
  }

  // 18) Prescribed-rate loan — split investment income -----------------------
  if (married && mr >= 0.4 && p.spouseNetIncome < ret.netIncome * 0.5 && hasInvestments) {
    push({
      id: 'income-splitting-loan', category: 'Income splitting', priority: 3,
      title: 'Split investment income with a prescribed-rate loan',
      mechanism: 'Moves investment income to a lower rate',
      where: 'A documented loan to your lower-income spouse at the CRA prescribed rate',
      how: 'Lend money to your spouse at the CRA’s prescribed rate (properly documented) and have them invest it. Best set up with a professional.',
      why: 'The investment income is then taxed in your spouse\'s lower bracket instead of yours — a legitimate way around the income-attribution rules.',
      impactLabel: 'household saving', citation: 'Income Tax Act s.74.5(2) · Prescribed-rate loan',
    });
  }

  // 19) Deduct investment carrying charges & interest ------------------------
  if (hasInvestments) {
    push({
      id: 'carrying-charges', category: 'Deductions', priority: 3,
      title: 'Deduct investment interest & carrying charges',
      mechanism: 'Lowers your taxable income',
      where: 'Your return (carrying charges, line 22100)',
      how: 'Track interest on investment loans and eligible investment-management or accounting fees, and claim them.',
      why: 'Interest on money borrowed to earn investment income — plus eligible advisory fees — is deductible, reducing your taxable income directly.',
      impactLabel: 'potential deduction', citation: 'Income Tax Act s.20(1)(c) · Interest & carrying charges',
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


/* ===== engine/verify.js ===== */
/**
 * ONYX Intelligence — verification & provenance
 * =========================================================================
 * Trust is earned through transparency and repeatable checks, not claims.
 * This module:
 *   - stamps every audit with an engine version + data-status,
 *   - runs a self-check against REFERENCE CASES whose expected federal/
 *     provincial/total tax are derived with the CRA's published method
 *     (brackets, basic personal amount, CPP/EI credits, the Canada employment
 *     amount, and the Ontario health premium). The arithmetic for each case is
 *     documented in the public methodology page.
 *
 * IMPORTANT: These checks prove the engine is internally consistent with the
 * documented method and hasn't drifted between deploys. They do NOT make the
 * engine CRA-certified. Software that FILES returns must pass the CRA's NETFILE
 * certification. This tool produces educational estimates only.
 * =========================================================================
 */


const ENGINE_VERSION = '1.0.0';

const DATA_STATUS = {
  2024: { label: 'Constants verified for the 2024 tax year', verified: true },
  2025: { label: 'Federal verified vs CRA (incl. the July 2025 rate cut to a blended 14.5%); ON, BC, AB & QC provincial figures verified vs official sources, other provinces preliminary (indexed)', verified: true },
};

// Each expected value is computed with the published CRA method; see methodology.html.
const REFERENCE_CASES = [
  {
    name: 'Ontario · employee · $60,000 · 2024',
    input: { province: 'ON', year: 2024, employmentIncome: 60000, cppContrib: 3361.75, eiContrib: 996 },
    // Federal: 15%×55,867 + 20.5%×4,133 = 9,227.32; credits 15%×(15,705+3,361.75+996+1,433)=3,224.36 → 6,002.95
    // Ontario: 5.05%×51,446 + 9.15%×8,554 = 3,380.71; credits 5.05%×(12,399+3,361.75+996)=846.22 → 2,534.49; +$600 health premium = 3,134.50
    expect: { federal: 6002.95, provincial: 3134.5, total: 9137.45 },
  },
  {
    name: 'Alberta · employee · $50,000 · 2024 (flat 10%)',
    input: { province: 'AB', year: 2024, employmentIncome: 50000, cppContrib: 2766.75, eiContrib: 830 },
    // Federal: 15%×50,000 = 7,500; credits 15%×(15,705+2,766.75+830+1,433)=3,110.21 → 4,389.79
    // Alberta: 10%×50,000 = 5,000; credits 10%×(21,885+2,766.75+830)=2,548.18 → 2,451.82
    expect: { federal: 4389.79, provincial: 2451.82, total: 6841.61 },
  },
  {
    name: 'British Columbia · employee · $100,000 · 2024 (multi-bracket)',
    input: { province: 'BC', year: 2024, employmentIncome: 100000, cppContrib: 4055.5, eiContrib: 1049.12 },
    // Federal: 15%×55,867 + 20.5%×44,133 = 17,427.32; credits 15%×(15,705+4,055.5+1,049.12+1,433)=3,336.39 → 14,090.92
    // BC: 5.06%×47,937 + 7.7%×47,938 + 10.5%×4,125 = 6,549.96; credits 5.06%×(12,580+4,055.5+1,049.12)=894.84 → 5,655.12
    expect: { federal: 14090.92, provincial: 5655.12, total: 19746.04 },
  },
  {
    // Full 2025 anchor: federal (blended 14.5% from the July 2025 cut) + Ontario
    // (verified 2025 provincial brackets, credits, and health premium).
    name: 'Ontario · employee · $60,000 · 2025 (blended 14.5%)',
    input: { province: 'ON', year: 2025, employmentIncome: 60000, cppContrib: 3361.75, eiContrib: 984 },
    // Federal: 14.5%×57,375 + 20.5%×2,625 = 8,857.50; credits 14.5%×(16,129+3,361.75+984+1,471)=3,182.13 → 5,675.37
    // Ontario: 5.05%×52,886 + 9.15%×7,114 = 3,321.67; credits 5.05%×(12,747+3,361.75+984)=863.18 → 2,458.49; +$600 health premium = 3,058.49
    expect: { federal: 5675.37, provincial: 3058.49, total: 8733.86 },
  },
];

const TOLERANCE = 1.5; // dollars — absorbs cent-level rounding differences

function selfCheck() {
  const checks = [];
  for (const c of REFERENCE_CASES) {
    const r = computeReturn(c.input);
    for (const field of Object.keys(c.expect)) {
      const got = field === 'total' ? r.tax.total : r.tax[field];
      const expected = c.expect[field];
      checks.push({ case: c.name, field, expected, got, pass: Math.abs(got - expected) <= TOLERANCE });
    }
  }
  const passCount = checks.filter((x) => x.pass).length;
  return {
    version: ENGINE_VERSION,
    ranAt: new Date().toISOString(),
    tolerance: TOLERANCE,
    total: checks.length,
    passCount,
    allPass: passCount === checks.length,
    checks,
  };
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
    // provenance: which engine + data produced this audit (shown to the user)
    engine: provenance(taxYear, ret.province, ret.provinceName),
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

/**
 * Build the provenance stamp for an audit. For 2025, the federal figures are
 * CRA-verified but provincial verification is per-jurisdiction: a province is
 * only "verified" once confirmed against its published amounts (verified2025).
 * dataVerified therefore requires BOTH the year's federal status AND, for that
 * province, its own verification flag.
 */
function provenance(taxYear, province, provinceName) {
  const status = DATA_STATUS[taxYear] || {};
  const provData = (getTaxData(taxYear).provinces || {})[province] || {};
  // 2024 constants are fully verified; 2025 depends on the province.
  const provinceVerified = taxYear === 2025 ? !!provData.verified2025 : !!status.verified;
  const dataVerified = !!status.verified && provinceVerified;
  let dataStatus = status.label || `${taxYear} constants`;
  if (taxYear === 2025) {
    dataStatus += provinceVerified
      ? ` — ${provinceName} provincial figures verified vs official source`
      : ` — ${provinceName} provincial figures preliminary (indexed estimate)`;
  }
  return {
    version: ENGINE_VERSION,
    dataStatus,
    dataVerified,
    provinceVerified,
    federalVerified: !!status.verified,
    methodologyUrl: 'methodology.html',
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

  if (path === '/meta') return { provinces: PROVINCE_NAMES, slipTypes: slipTypes, years: (typeof AVAILABLE_YEARS !== 'undefined' ? AVAILABLE_YEARS : [2024]), year: 2024, engineVersion: (typeof ENGINE_VERSION !== 'undefined' ? ENGINE_VERSION : '1.0.0') };
  if (path === '/verify') return selfCheck();

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
