/**
 * Assurance & reliability framework tests.
 * Confirms the reconciliation checks fire correctly and confidence is
 * monotonic in data quality (clean returns score higher than messy ones).
 */
'use strict';

const assert = require('assert');
const { runAudit } = require('../engine');
const { runConsistencyChecks } = require('../engine/assurance');
const { computeReturn } = require('../engine/taxEngine');

let passed = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); console.log('  ✓ ' + msg); passed++; };
const has = (checks, id, status) => checks.some((c) => c.id === id && (!status || c.status === status));

console.log('Assurance framework:');

// 1) Clean, fully-documented T4 reconciles and scores high.
const clean = runAudit({
  profile: { province: 'ON', year: 2025 },
  documents: [{ type: 'T4', fields: { employmentIncome: 68000, cppContrib: 3867.5, eiContrib: 1049.12, taxWithheld: 11800 } }],
});
ok(clean.assurance, 'audit carries an assurance report');
ok(clean.assurance.confidence.score >= 85, `clean documented return has high confidence (${clean.assurance.confidence.score})`);
ok(has(clean.assurance.checks, 'cpp', 'pass'), 'CPP reconciles for a clean T4');
ok(has(clean.assurance.checks, 'ei', 'pass'), 'EI reconciles for a clean T4');
ok(has(clean.assurance.checks, 'tie-out', 'pass'), 'return ties out (federal + provincial = total)');

// 2) Missing payroll figures are flagged for review.
const noPayroll = computeReturn({ province: 'ON', year: 2025, employmentIncome: 70000, cppContrib: 0, eiContrib: 0, taxWithheld: 0 });
const npChecks = runConsistencyChecks(noPayroll);
ok(has(npChecks, 'cpp', 'review'), 'blank CPP on employment income is flagged for review');
ok(has(npChecks, 'ei', 'review'), 'blank EI on employment income is flagged for review');
ok(has(npChecks, 'withholding', 'review'), 'zero withholding on high income is flagged for review');

// 3) CPP over the annual maximum is a hard flag (recoverable over-contribution).
const overCpp = computeReturn({ province: 'ON', year: 2025, employmentIncome: 90000, cppContrib: 6000, eiContrib: 1077.48, taxWithheld: 15000 });
ok(has(runConsistencyChecks(overCpp), 'cpp', 'flag'), 'CPP above the maximum is flagged');

// 4) Genuinely out-of-scope income is disclosed as a flag; foreign stays out of scope.
const scoped = computeReturn({ province: 'BC', year: 2025, employmentIncome: 60000, cppContrib: 3388, eiContrib: 984, taxWithheld: 9000, hasForeignIncome: true });
ok(has(runConsistencyChecks(scoped), 'scope-foreign', 'flag'), 'foreign income is disclosed as out of scope');

// 4b) Rental & self-employment are now modelled, not dropped.
const rental = computeReturn({ province: 'ON', year: 2025, employmentIncome: 70000, cppContrib: 3867.5, eiContrib: 1077.48, taxWithheld: 12000, rentalIncome: 24000, rentalExpenses: 9000 });
ok(rental.income.rental === 15000, 'net rental income is included in the return');
ok(has(runConsistencyChecks(rental), 'scope-rental', 'pass'), 'entered rental income reconciles (no longer a flag)');
const selfEmp = computeReturn({ province: 'ON', year: 2025, selfEmploymentIncome: 60000 });
ok(selfEmp.tax.cppPayableSE > 6000, `self-employed CPP (both portions) is computed (${selfEmp.tax.cppPayableSE})`);
ok(selfEmp.deductions.selfEmployedCpp > 3000, 'half the self-employed CPP is deducted');

// 5) Confidence is monotonic: clean > messy.
const messy = runAudit({
  profile: { province: 'BC', year: 2025, hasInvestments: true, hasRentalIncome: true, isStudent: true },
  financial: { employmentIncome: 90000, cppContrib: 0, eiContrib: 0, taxWithheld: 0 },
});
ok(messy.assurance.confidence.score < clean.assurance.confidence.score, `messy manual return scores lower (${messy.assurance.confidence.score} < ${clean.assurance.confidence.score})`);
ok(messy.assurance.counts.flag + messy.assurance.counts.review >= 4, 'messy return surfaces several items to resolve');

// 6) Determinism: same input → same confidence.
const again = runAudit({
  profile: { province: 'ON', year: 2025 },
  documents: [{ type: 'T4', fields: { employmentIncome: 68000, cppContrib: 3867.5, eiContrib: 1049.12, taxWithheld: 11800 } }],
});
ok(again.assurance.confidence.score === clean.assurance.confidence.score, 'confidence is deterministic');

// 7) Assumptions are personalized (capital gains → inclusion-rate assumption present).
const withGains = runAudit({ profile: { province: 'ON', year: 2025 }, financial: { employmentIncome: 60000, capitalGains: 10000, cppContrib: 3361.75, eiContrib: 984, taxWithheld: 8000 } });
ok(withGains.assurance.assumptions.some((a) => /50% inclusion/i.test(a.text)), 'capital-gains assumption is listed when gains are present');

console.log(`\n✓ Assurance: ${passed} assertions passed.`);
