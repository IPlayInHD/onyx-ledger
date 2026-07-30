/**
 * ONYX Intelligence — planning tools (QOL features)
 *   - optimizeRRSP : solve the RRSP contribution to erase a balance owing or
 *                    drop into a lower tax bracket (recomputed with the engine)
 *   - estimateBenefits : rough estimates of refundable government benefits
 *                    (GST/HST credit, Canada Carbon Rebate, Canada Child Benefit)
 *   - taxCalendar : the taxpayer's next key CRA deadlines, with live countdowns
 * All estimates are clearly labelled; benefits use simplified formulas.
 * =========================================================================
 */
'use strict';

const { computeReturn } = require('./taxEngine');

const r0 = (n) => Math.round(n);
const ceilTo = (n, step) => Math.ceil(n / step) * step;

/** Solve useful RRSP contributions using the real engine. */
function optimizeRRSP(ret, estRoom) {
  const base = ret.profile;
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
      type: 'erase', contribution: r0(Math.min(guess, roomLeft)),
      newRefund: after.refundOrBalance,
      label: `Contribute about $${r0(Math.min(guess, roomLeft)).toLocaleString('en-CA')} to wipe out your balance owing`,
      note: 'Brings your estimated balance close to zero at your marginal rate.',
    });
  }

  // 2) Drop into the next-lower federal bracket.
  const thresholds = [55867, 111733, 173205, 246752];
  const rateAbove = { 55867: '20.5%', 111733: '26%', 173205: '29%', 246752: '33%' };
  const rateBelow = { 55867: '15%', 111733: '20.5%', 173205: '26%', 246752: '29%' };
  const ti = ret.taxableIncome;
  const th = thresholds.filter((t) => t < ti - 1).pop();
  if (th) {
    const need = ceilTo(ti - th, 100);
    if (need > 0 && need <= roomLeft) {
      const after = recompute(need);
      moves.push({
        type: 'bracket', contribution: r0(need), newRefund: after.refundOrBalance,
        label: `Contribute $${r0(need).toLocaleString('en-CA')} to drop from the ${rateAbove[th]} bracket into the ${rateBelow[th]} bracket`,
        note: 'Lowers the tax rate on your top dollars of income.',
      });
    }
  }

  return moves.map((m) => Object.assign(m, { delta: r0(m.newRefund - ret.refundOrBalance), roomLeft: r0(roomLeft) }));
}

/** Rough estimates of refundable government benefits (paid separately from a refund). */
function estimateBenefits(ret) {
  const p = ret.profile;
  const net = ret.netIncome;
  const kids = p.dependants || 0;
  const married = p.maritalStatus === 'married' || p.maritalStatus === 'commonlaw';
  const out = [];

  // GST/HST credit (approximate 2024 base year)
  const gstBase = 340 + (married ? 179 : 0) + kids * 179;
  const gst = Math.max(0, gstBase - 0.05 * Math.max(0, net - 45000));
  if (gst > 20) out.push({ id: 'gst', label: 'GST/HST credit', amount: r0(gst), note: 'Quarterly, tax-free — for lower-income individuals and families.' });

  // Canada Carbon Rebate (fuel-charge provinces only; flat, not income-tested)
  const CCR = { AB: 900, SK: 752, MB: 600, ON: 560, NB: 380, NS: 412, PE: 440, NL: 596 };
  if (CCR[p.province]) {
    const b = CCR[p.province];
    const ccr = b + (married ? b * 0.5 : 0) + kids * b * 0.25;
    out.push({ id: 'ccr', label: 'Canada Carbon Rebate', amount: r0(ccr), note: 'Quarterly, tax-free — automatic when you file in an eligible province.' });
  }

  // Canada Child Benefit (approximate 2024-25)
  if (kids > 0) {
    let ccb = kids * 6570;
    const rate = kids === 1 ? 0.07 : kids === 2 ? 0.135 : kids === 3 ? 0.19 : 0.23;
    ccb -= rate * Math.max(0, net - 36502);
    ccb = Math.max(0, ccb);
    if (ccb > 50) out.push({ id: 'ccb', label: 'Canada Child Benefit', amount: r0(ccb), note: 'Monthly, tax-free — based on family net income and number of children.' });
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
    .map((r) => {
      const date = nextOccur(r.m, r.d);
      return { label: r.label, tag: r.tag, note: r.note, date: date.toISOString().slice(0, 10), daysAway: Math.round((date - today) / oneDay) };
    })
    .sort((a, b) => a.daysAway - b.daysAway)
    .slice(0, 6);
}

module.exports = { optimizeRRSP, estimateBenefits, taxCalendar };
