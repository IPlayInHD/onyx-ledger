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

  // 20) Self-employment: deduct every eligible business expense --------------
  if (p.selfEmploymentIncome > 0) {
    const noExp = !p.selfEmploymentExpenses;
    push({
      id: 'self-emp-expenses', category: 'Deductions', priority: noExp ? 1 : 3,
      title: noExp ? 'Deduct your business expenses' : 'Keep claiming every business expense',
      mechanism: 'Lowers your taxable income',
      where: 'Form T2125 (business income & expenses) on your return',
      how: 'Track and deduct supplies, a reasonable share of vehicle costs, phone/internet, professional fees, and business-use-of-home (a portion of rent/utilities). Consider capital cost allowance on equipment.',
      why: noExp
        ? 'No business expenses were entered. Every eligible expense comes straight off your self-employment income, so it saves tax at your full marginal rate — and reduces your self-employed CPP too.'
        : 'Each eligible expense reduces your self-employment income directly, lowering both income tax and the self-employed CPP you owe on it.',
      impact: noExp ? money(Math.min(p.selfEmploymentIncome * 0.1, 8000) * (mr + 0.06)) : null,
      impactLabel: noExp ? 'est. tax + CPP saved' : 'ongoing deduction',
      citation: 'Income Tax Act s.9 / s.18 · Business income & expenses (T2125)',
    });
  }

  // 21) Rental: deduct expenses & consider CCA -------------------------------
  if (p.rentalIncome > 0 || ret.income.rental !== 0) {
    push({
      id: 'rental-expenses', category: 'Deductions', priority: 3,
      title: 'Deduct your rental expenses (and consider CCA)',
      mechanism: 'Lowers your taxable income',
      where: 'Form T776 (statement of real-estate rentals)',
      how: 'Deduct mortgage interest, property tax, insurance, repairs, condo fees, and management. Capital cost allowance (depreciation) can further reduce net rental income — but plan it, as it can be recaptured on sale.',
      why: 'Rental income is taxed on the net amount, so every eligible expense reduces it directly at your marginal rate. CCA is optional and best used deliberately.',
      impactLabel: 'deduction / timing', citation: 'Income Tax Act s.20(1)(a) · Rental expenses & CCA (T776)',
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
  if (i.selfEmployment !== 0) f.push({ ok: true, label: 'Self-employment (net)', value: fmt(i.selfEmployment) });
  if (i.rental !== 0) f.push({ ok: true, label: 'Rental income (net)', value: fmt(i.rental) });
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
      'ONYX runs the CRA’s published federal and provincial method and reconciles your figures the way an accountant would before sign-off — see Assurance & reliability above for this audit’s confidence score and cross-checks. Treat it as a professional-grade analysis of the information you provided; its reliability follows that confidence score. It is not a return filed with the CRA — ONYX does not transmit returns, which requires NETFILE certification — so confirm the specifics before you file. ONYX is not affiliated with the CRA.',
  };
}

module.exports = { findOpportunities, whatWeFound, buildAdvisory };
