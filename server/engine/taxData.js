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

'use strict';

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

module.exports = { TAX_DATA, getTaxData, PROVINCE_NAMES, AVAILABLE_YEARS, DEFAULT_YEAR: 2024 };
