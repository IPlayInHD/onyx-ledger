/**
 * ONYX reference backtests — validate the engine against documented reference
 * cases and confirm determinism. Run: node test/reference.test.js
 */
'use strict';

const assert = require('assert');
const { selfCheck } = require('../engine/verify');
const { computeReturn } = require('../engine/taxEngine');

let passed = 0;
const ok = (c, m) => { assert.ok(c, m); passed++; };

// 1) Reference cases (hand-derived expected federal / provincial / total).
const sc = selfCheck();
console.log(`\nReference self-check (engine v${sc.version}, tolerance ±$${sc.tolerance}):`);
sc.checks.forEach((ch) => {
  console.log(`  ${ch.pass ? '✓' : '✗'} ${ch.case} · ${ch.field}: expected $${ch.expected}, got $${ch.got}`);
  ok(ch.pass, `${ch.case} — ${ch.field}`);
});
ok(sc.allPass, 'all reference checks pass');

// 2) Determinism — identical inputs must produce byte-identical outputs.
const inp = { province: 'ON', year: 2024, employmentIncome: 73210, eligibleDividends: 2000, rrspDeduction: 4000, medicalExpenses: 1500, donations: 400, taxWithheld: 9000 };
ok(JSON.stringify(computeReturn(inp)) === JSON.stringify(computeReturn(inp)), 'engine is deterministic (identical inputs → identical outputs)');

// 3) Cross-run stability across all provinces (no hidden state / order dependence).
const codes = Object.keys(require('../engine/taxData').getTaxData(2024).provinces);
let stable = true;
codes.forEach((p) => {
  const one = JSON.stringify(computeReturn({ province: p, year: 2024, employmentIncome: 85000, taxWithheld: 0 }));
  const two = JSON.stringify(computeReturn({ province: p, year: 2024, employmentIncome: 85000, taxWithheld: 0 }));
  if (one !== two) stable = false;
});
ok(stable, 'every province computes stably on repeat');

console.log(`\n✓ Reference backtests: ${passed} assertions passed (${sc.passCount}/${sc.total} reference checks).`);
