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
'use strict';

const { computeReturn } = require('./taxEngine');

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

module.exports = { selfCheck, ENGINE_VERSION, DATA_STATUS, REFERENCE_CASES };
