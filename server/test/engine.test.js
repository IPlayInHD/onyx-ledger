/**
 * ONYX engine test suite — verifies the tax math against hand-computed cases.
 * Run: node test/engine.test.js   (no framework; exits non-zero on failure)
 */
'use strict';

const assert = require('assert');
const { bracketTax } = require('../engine/taxEngine');
const { getTaxData } = require('../engine/taxData');
const { runAudit, computeReturn } = require('../engine');

let passed = 0;
const near = (a, b, tol, msg) => { assert.ok(Math.abs(a - b) <= tol, `${msg} — expected ~${b}, got ${a}`); passed++; };
const ok = (c, msg) => { assert.ok(c, msg); passed++; };

const fed = getTaxData(2024).federal;

// ---- 1. Bracket math ----
near(bracketTax(60000, fed.brackets), 9227.315, 0.5, 'Federal bracket tax on $60,000');
near(bracketTax(55867, fed.brackets), 8380.05, 0.5, 'Federal tax exactly at first threshold');
near(bracketTax(0, fed.brackets), 0, 0.001, 'Zero income → zero tax');
near(bracketTax(200000, fed.brackets),
  0.15 * 55867 + 0.205 * (111733 - 55867) + 0.26 * (173205 - 111733) + 0.29 * (200000 - 173205),
  0.5, 'Federal tax across four brackets');

// ---- 2. Full case: Ontario employee, $60,000 (hand-computed) ----
const on = computeReturn({
  province: 'ON', year: 2024, employmentIncome: 60000,
  cppContrib: 3361.75, eiContrib: 996, taxWithheld: 9000,
});
near(on.tax.federal, 6002.95, 1.5, 'ON $60k federal tax');
near(on.tax.provincial, 3134.5, 1.5, 'ON $60k provincial tax (incl. health premium)');
near(on.tax.healthPremium, 600, 0.01, 'ON health premium at $60k');
near(on.tax.total, 9137.45, 2, 'ON $60k total income tax');
near(on.refundOrBalance, -137.45, 2, 'ON $60k balance owing');
ok(on.marginalRate > 0.29 && on.marginalRate < 0.32, `ON $60k marginal rate sane (${on.marginalRate})`);

// ---- 3. Flat-rate province: Alberta, $50,000 ----
const ab = computeReturn({ province: 'AB', year: 2024, employmentIncome: 50000, cppContrib: 2767.75, eiContrib: 830, taxWithheld: 8000 });
// AB provincial before credits = 10% of 50,000 = 5,000; credit ~10% of (21,885+2767.75+830)=2,548 → ~2,452
near(ab.tax.provincialBeforeCredits, 5000, 0.5, 'AB provincial before credits = flat 10%');
ok(ab.tax.provincial > 2000 && ab.tax.provincial < 2700, `AB provincial tax after credits sane (${ab.tax.provincial})`);

// ---- 4. Quebec abatement reduces federal tax vs an equivalent ROC filer ----
const qcInput = { year: 2024, employmentIncome: 80000, cppContrib: 3867.5, eiContrib: 1049.12, taxWithheld: 12000 };
const qc = computeReturn(Object.assign({ province: 'QC' }, qcInput));
const onEq = computeReturn(Object.assign({ province: 'ON' }, qcInput));
ok(qc.tax.federal < onEq.tax.federal, 'Quebec federal tax is reduced by the 16.5% abatement');

// ---- 5. Refund direction: lots withheld → refund ----
const refundCase = computeReturn({ province: 'BC', year: 2024, employmentIncome: 45000, cppContrib: 2469.25, eiContrib: 747, taxWithheld: 9000 });
ok(refundCase.isRefund && refundCase.refundOrBalance > 0, `Over-withholding produces a refund (${refundCase.refundOrBalance})`);

// ---- 6. RRSP deduction lowers tax at the marginal rate ----
const noRrsp = computeReturn({ province: 'ON', year: 2024, employmentIncome: 90000, taxWithheld: 0 });
const withRrsp = computeReturn({ province: 'ON', year: 2024, employmentIncome: 90000, rrspDeduction: 10000, taxWithheld: 0 });
const savedPerDollar = (noRrsp.tax.total - withRrsp.tax.total) / 10000;
ok(savedPerDollar > 0.28 && savedPerDollar < 0.38, `$10k RRSP saves ~marginal rate per dollar (${savedPerDollar.toFixed(3)})`);

// ---- 7. Full audit pipeline with documents ----
const audit = runAudit({
  profile: { province: 'ON', year: 2024, age: 34, maritalStatus: 'single', isStudent: true, firstTimeHomeBuyer: true, ownsHome: false },
  documents: [
    { type: 'T4', fields: { employmentIncome: 68000, cppContrib: 3867.5, eiContrib: 1049.12, taxWithheld: 11800 } },
    { type: 'T2202', fields: { tuition: 4200 } },
    { type: 'RRSP', fields: { rrspDeduction: 3000 } },
    { type: 'DONATION', fields: { donations: 500 } },
  ],
});
ok(audit.return.income.employment === 68000, 'Audit extracted employment income from T4');
ok(audit.return.credits.tuition === 4200, 'Audit extracted tuition from T2202');
ok(audit.health.score >= 0 && audit.health.score <= 100, `Health score in range (${audit.health.score})`);
ok(Array.isArray(audit.opportunities) && audit.opportunities.length > 0, `Opportunities generated (${audit.opportunities.length})`);
ok(audit.opportunities.some((o) => o.id === 'fhsa'), 'FHSA opportunity fires for eligible first-time buyer');
ok(audit.opportunities.some((o) => o.id === 'tuition'), 'Tuition optimization opportunity fires');
ok(audit.advisory.headline.includes('/100'), 'Advisory headline references the score');
ok(typeof audit.advisory.estimatedOpportunity === 'number', 'Advisory totals a dollar opportunity');

// ---- 8. Text/OCR extraction path ----
const textAudit = runAudit({
  profile: { province: 'AB', year: 2024 },
  documents: [{ type: 'T4', text: 'Statement of Remuneration Paid\nBox 14 Employment income  72,500.00\nBox 22 Income tax deducted  12,340.00' }],
});
near(textAudit.return.income.employment, 72500, 0.01, 'OCR text scan pulled Box 14 employment income');
near(textAudit.return.taxWithheld, 12340, 0.01, 'OCR text scan pulled Box 22 tax withheld');

// ---- 8b. QOL: checklist, RRSP optimizer, benefits, calendar ----
const { buildChecklist } = require('../engine/checklist');
const { optimizeRRSP, estimateBenefits, taxCalendar } = require('../engine/planner');

// Self-employed investor with no docs → checklist flags missing self-employment + investment records
const selfAudit = runAudit({
  profile: { province: 'ON', year: 2024, employmentType: 'self-employed', hasInvestments: true, maritalStatus: 'single' },
  financial: { selfEmploymentIncome: 0 },
});
ok(selfAudit.checklist.items.some((x) => x.id === 'self' && x.status === 'missing'), 'checklist flags missing self-employment records');
ok(selfAudit.checklist.items.some((x) => x.id === 'invest' && x.status === 'missing'), 'checklist flags missing investment slips');
ok(selfAudit.checklist.completeness >= 0 && selfAudit.checklist.completeness <= 100, `checklist completeness in range (${selfAudit.checklist.completeness}%)`);

// RRSP optimizer erases a balance owing
const owing = computeReturn({ province: 'ON', year: 2024, employmentIncome: 95000, taxWithheld: 12000 });
ok(!owing.isRefund, 'setup: $95k with low withholding owes tax');
const moves = optimizeRRSP(owing, 20000);
ok(moves.length > 0, `RRSP optimizer returns moves (${moves.length})`);
ok(moves.some((m) => m.type === 'erase') || moves.some((m) => m.type === 'bracket'), 'optimizer suggests erase-owing and/or bracket-drop');
const eraseMove = moves.find((m) => m.type === 'erase');
if (eraseMove) ok(eraseMove.newRefund > owing.refundOrBalance, 'erase move improves the balance');

// Benefits: a family gets CCB + GST estimates
const family = computeReturn({ province: 'ON', year: 2024, employmentIncome: 42000, dependants: 2, maritalStatus: 'married', spouseNetIncome: 0 });
const ben = estimateBenefits(family);
ok(ben.items.some((b) => b.id === 'ccb'), 'benefits estimate includes Canada Child Benefit for a family');
ok(ben.total > 0, `benefits total > 0 ($${ben.total})`);

// Calendar: returns upcoming, future-dated deadlines
const cal = taxCalendar({ employmentType: 'self-employed' });
ok(cal.length > 0 && cal.every((c) => c.daysAway >= 0), 'calendar returns only upcoming deadlines');
ok(cal.some((c) => c.tag === 'instalment'), 'self-employed calendar includes instalment deadlines (personalized)');
ok(taxCalendar({ employmentType: 'employed' }).every((c) => c.tag !== 'instalment'), 'employee calendar omits instalments');

// ---- 8c. 2025 tax year + bracket/cashflow + simulate + account priority ----
const fed25 = getTaxData(2025).federal;
near(bracketTax(60000, fed25.brackets), 0.15 * 57375 + 0.205 * (60000 - 57375), 0.5, '2025 federal bracket tax on $60,000');
const r2025 = computeReturn({ province: 'ON', year: 2025, employmentIncome: 60000, cppContrib: 4034.1, eiContrib: 996, taxWithheld: 9000 });
ok(r2025.tax.total > 0 && Math.abs(r2025.tax.total - on.tax.total) > 1, '2025 differs from 2024 (indexation)');
ok(r2025.bracketFederal && r2025.bracketFederal.rate === 0.205, 'bracketFederal identifies the 20.5% band at $60k');
ok(r2025.bracketFederal.toNext > 0, `bracketFederal reports distance to next bracket (${r2025.bracketFederal.toNext})`);
ok(Math.abs(r2025.cashflow.takeHome - (r2025.income.total - r2025.tax.total - r2025.cashflow.cpp - r2025.cashflow.ei)) < 1, 'cashflow.takeHome = income − tax − CPP − EI');

const simBase = { province: 'ON', year: 2024, employmentIncome: 95000, taxWithheld: 12000 };
const sim0 = require('../engine').simulate({ profile: simBase, overrides: {} });
const sim1 = require('../engine').simulate({ profile: simBase, overrides: { rrsp: 10000 } });
ok(sim1.refundOrBalance > sim0.refundOrBalance, 'simulate: adding $10k RRSP improves the position');
ok(sim1.taxableIncome === sim0.taxableIncome - 10000, 'simulate: RRSP override reduces taxable income');

const { accountPriority } = require('../engine/planner');
const ap = accountPriority(computeReturn({ province: 'ON', year: 2024, employmentIncome: 90000, firstTimeHomeBuyer: true, ownsHome: false }));
ok(ap.order.includes('FHSA') && ap.order.includes('RRSP'), 'account priority recommends FHSA + RRSP for a high-earning first-time buyer');

// ---- 9. Every province computes without error ----
for (const code of Object.keys(getTaxData(2024).provinces)) {
  const r = computeReturn({ province: code, year: 2024, employmentIncome: 75000, cppContrib: 3867.5, eiContrib: 1049.12, taxWithheld: 12000 });
  ok(r.tax.total > 0 && r.tax.total < 75000, `${code}: total tax is sane (${r.tax.total})`);
}

console.log(`\n✓ All ${passed} assertions passed.`);
