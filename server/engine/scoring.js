/**
 * ONYX Intelligence — Tax Health scoring
 * =========================================================================
 * A 0–100 score that mirrors how a tax professional would rate a filer's
 * position, across five weighted categories. Each sub-score is derived from
 * the computed return + profile + scanned documents, then combined.
 *
 * This is an educational assessment of tax *optimization and hygiene*, not an
 * audit-risk score or a compliance guarantee.
 * =========================================================================
 */

'use strict';

const { getTaxData } = require('./taxData');

const clamp = (n, lo = 0, hi = 100) => Math.max(lo, Math.min(hi, n));
const pct = (n) => Math.round(clamp(n));

/** Estimate RRSP room if the user hasn't supplied it (18% of earned income, capped). */
function estimateRrspRoom(profile, data) {
  const earned = profile.employmentIncome + profile.selfEmploymentIncome;
  return Math.min(earned * data.federal.rrspRoomRate, data.federal.rrspRoomCap);
}

function computeTaxHealth(ret) {
  const p = ret.profile;
  const data = getTaxData(p.year);
  const notes = [];

  // 1) Registered savings utilization (RRSP / TFSA / FHSA) ---------------- 30
  const estRoom = p.rrspRoom != null ? p.rrspRoom + p.rrspDeduction : estimateRrspRoom(p, data);
  const rrspUtil = estRoom > 0 ? p.rrspDeduction / estRoom : (p.employmentIncome + p.selfEmploymentIncome > 0 ? 0 : 1);
  let savings = rrspUtil * 70;
  // FHSA opportunity for eligible first-time buyers
  if (p.firstTimeHomeBuyer && !p.ownsHome) savings += (p.fhsaDeduction >= data.federal.fhsaAnnual ? 15 : p.fhsaDeduction > 0 ? 8 : 0);
  else savings += 12;
  // TFSA signal
  if (p.tfsaRoom != null) savings += p.tfsaRoom > 20000 ? 4 : 12;
  else savings += 10;
  savings = clamp(savings);
  if (rrspUtil < 0.5 && estRoom > 1000) notes.push('Significant unused RRSP room.');

  // 2) Deduction & credit capture ---------------------------------------- 25
  let capture = 55; // baseline: engine already applies the automatic credits
  if (p.donations > 0) capture += 6;
  if (p.medicalExpenses > 0) capture += 6;
  if (p.isStudent && p.tuition > 0) capture += 8; else if (p.isStudent && p.tuition === 0) { capture -= 6; notes.push('Student with no tuition captured.'); }
  if (p.dependants > 0 && p.childCare > 0) capture += 8; else if (p.dependants > 0 && p.childCare === 0) { capture -= 6; notes.push('Dependants but no child-care expenses claimed.'); }
  if (p.employmentIncome > 0 && p.employmentExpenses > 0) capture += 4;
  if (p.unionDues > 0) capture += 3;
  capture = clamp(capture);

  // 3) Tax efficiency ---------------------------------------------------- 15
  // Reward a sensible gap between marginal and average rate (planning headroom used),
  // penalize a large balance owing relative to income.
  let efficiency = 60;
  efficiency += clamp((ret.marginalRate - ret.averageRate) * 1.5, 0, 20);
  if (!ret.isRefund && ret.income.total > 0) {
    const owingRatio = Math.abs(ret.refundOrBalance) / ret.income.total;
    if (owingRatio > 0.08) { efficiency -= 25; notes.push('Large balance owing relative to income.'); }
    else if (owingRatio > 0.02) efficiency -= 12;
  }
  if (rrspUtil >= 0.8) efficiency += 8;
  efficiency = clamp(efficiency);

  // 4) Documentation & compliance ---------------------------------------- 15
  const ds = ret._docSummary || null;
  let documentation;
  if (ds && ds.total > 0) {
    documentation = ds.avgConfidence * 100 * 0.7;
    documentation += (ds.processed / ds.total) * 30;
    if (ds.failed > 0) documentation -= ds.failed * 8;
    if (ds.needsReview > 0) documentation -= ds.needsReview * 4;
  } else {
    documentation = 45; // no documents scanned yet
    notes.push('No documents scanned — audit is based on entered figures only.');
  }
  documentation = clamp(documentation);

  // 5) Planning & carryforwards ------------------------------------------ 15
  let planning = 50;
  if ((p.maritalStatus === 'married' || p.maritalStatus === 'commonlaw')) {
    planning += 8; // household optimization available
    if (p.age && p.age >= 65 && p.pensionIncome > 0) planning += 8; // pension splitting
  }
  if (p.firstTimeHomeBuyer && !p.ownsHome && p.fhsaDeduction > 0) planning += 8;
  if (rrspUtil >= 0.6) planning += 10;
  if (p.tfsaRoom != null && p.tfsaRoom < 10000) planning += 6;
  planning = clamp(planning);

  const subscores = [
    { key: 'savings', label: 'Registered savings', score: pct(savings), weight: 30 },
    { key: 'capture', label: 'Deduction & credit capture', score: pct(capture), weight: 25 },
    { key: 'efficiency', label: 'Tax efficiency', score: pct(efficiency), weight: 15 },
    { key: 'documentation', label: 'Documentation', score: pct(documentation), weight: 15 },
    { key: 'planning', label: 'Planning', score: pct(planning), weight: 15 },
  ];

  const score = Math.round(subscores.reduce((s, c) => s + (c.score * c.weight) / 100, 0));
  const band =
    score >= 85 ? { label: 'Excellent', tone: 'positive' } :
    score >= 70 ? { label: 'Good', tone: 'positive' } :
    score >= 50 ? { label: 'Fair', tone: 'warning' } :
                  { label: 'Needs attention', tone: 'critical' };

  return { score, band, subscores, notes, context: { rrspUtil: Math.round(rrspUtil * 100) / 100, estimatedRrspRoom: Math.round(estRoom) } };
}

module.exports = { computeTaxHealth, estimateRrspRoom };
