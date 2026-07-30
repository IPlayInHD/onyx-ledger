/**
 * ONYX Intelligence — planning tools (QOL)
 *   - optimizeRRSP     : solve the RRSP contribution to erase owing / drop a bracket
 *   - accountPriority  : which registered account to prioritize (RRSP/TFSA/FHSA)
 *   - estimateBenefits : rough estimates of refundable government benefits
 *   - taxCalendar      : the taxpayer's next key CRA deadlines, with countdowns
 * Estimates are clearly labelled; benefit formulas are simplified.
 */
'use strict';

const { computeReturn } = require('./taxEngine');
const { getTaxData } = require('./taxData');

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

module.exports = { optimizeRRSP, accountPriority, estimateBenefits, taxCalendar };
