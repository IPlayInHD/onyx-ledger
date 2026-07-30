/**
 * ONYX Intelligence — personalized document checklist
 * =========================================================================
 * Compares the taxpayer's situation (profile) against what they've actually
 * provided (extracted figures + uploaded document types) to flag the records
 * ONYX still needs — the "documents to add" an auditor would ask for.
 * =========================================================================
 */
'use strict';

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

module.exports = { buildChecklist };
