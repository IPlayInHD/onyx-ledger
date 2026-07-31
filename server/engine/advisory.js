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

'use strict';

const { getTaxData } = require('./taxData');

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

  // ---- Coach-style strategies (established Canadian tax-saving moves) ----
  const married = p.maritalStatus === 'married' || p.maritalStatus === 'commonlaw';
  const incomeGap = ret.netIncome - (married ? p.spouseNetIncome : ret.netIncome);
  const hasInvestments = p.hasInvestments || (p.interestIncome + p.eligibleDividends + p.nonEligibleDividends) > 0 || p.capitalGains > 0;

  // 15) Spousal RRSP — split income into retirement --------------------------
  if (married && incomeGap > 15000 && (p.employmentIncome + p.selfEmploymentIncome) > 0) {
    push({
      id: 'spousal-rrsp', category: 'Planning', priority: 1,
      title: 'Use a spousal RRSP to split future income',
      detail: 'Because you earn more than your spouse, contributing to a spousal RRSP lets you take the deduction now at your higher rate, while the withdrawals are taxed in your spouse’s hands later — usually at a lower rate. It uses your own RRSP room, so it stacks with your regular RRSP.',
      impact: money(Math.min(roomLeft || 6000, 6000) * Math.max(mr - 0.2, 0.05)), impactLabel: 'est. household saving',
      citation: 'Income Tax Act s.146(5.1) · Spousal RRSP',
    });
  }

  // 16) Home Buyers' Plan — stack with FHSA ----------------------------------
  if (p.firstTimeHomeBuyer && !p.ownsHome) {
    push({
      id: 'hbp', category: 'Registered savings', priority: 2,
      title: 'Stack the Home Buyers’ Plan with your FHSA',
      detail: 'As a first-time buyer you can also withdraw up to $60,000 from your RRSP tax-free under the Home Buyers’ Plan (repaid over 15 years). Combined with an FHSA, that’s a much larger tax-advantaged down payment — and RRSP contributions still give you a deduction on the way in.',
      impactLabel: 'tax-free withdrawal', citation: 'Income Tax Act s.146.01 · Home Buyers’ Plan',
    });
  }

  // 17) Donate appreciated securities in-kind --------------------------------
  if (hasInvestments && (p.donations > 0 || p.capitalGains > 0)) {
    const avoided = p.capitalGains > 0 ? money(p.capitalGains * data.federal.capitalGainsInclusion * mr) : null;
    push({
      id: 'donate-securities', category: 'Credits', priority: 2,
      title: 'Donate appreciated stock instead of cash',
      detail: 'Donating publicly-traded shares or ETFs directly to a registered charity eliminates the capital-gains tax on them entirely, and you still get the full donation credit on the fair market value — one of the most tax-efficient ways to give.',
      impact: avoided, impactLabel: avoided ? 'est. gains tax avoided' : 'gains-tax-free giving',
      citation: 'Income Tax Act s.38(a.1) · Gifts of listed securities',
    });
  }

  // 18) Prescribed-rate loan — split investment income -----------------------
  if (married && mr >= 0.4 && p.spouseNetIncome < ret.netIncome * 0.5 && hasInvestments) {
    push({
      id: 'income-splitting-loan', category: 'Planning', priority: 3,
      title: 'Split investment income with a prescribed-rate loan',
      detail: 'With a large income gap and taxable investments, lending money to your lower-income spouse at the CRA’s prescribed rate lets the investment income be taxed in their hands, at a lower rate — a legitimate way around the attribution rules. Best set up with a professional.',
      impactLabel: 'household saving', citation: 'Income Tax Act s.74.5(2) · Prescribed-rate loan',
    });
  }

  // 19) Deduct investment carrying charges & interest ------------------------
  if (hasInvestments) {
    push({
      id: 'carrying-charges', category: 'Deductions', priority: 3,
      title: 'Deduct investment interest & carrying charges',
      detail: 'Interest on money borrowed to earn investment income is generally tax-deductible, as are eligible investment-management and accounting fees. If you have an investment loan or advisory fees, track them — they reduce your taxable income directly.',
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

module.exports = { findOpportunities, whatWeFound, buildAdvisory };
