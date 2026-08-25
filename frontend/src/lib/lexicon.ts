/* =========================================================================
   THE CONSUMER LEXICON
   =========================================================================
   Onyx's engine speaks in closed governed vocabularies — `EVIDENCE_REQUIRED`,
   `RESERVED_NO_PRODUCER`, `INCREASE_RRSP_DEDUCTION`. Those names are correct
   and several are load-bearing. None of them is something to show a person who
   has never filed a tax return.

   WHAT THIS REPLACES. Until now the product rendered those codes through
   `humanize()`, which lowercases a string and replaces underscores. That is
   not a translation: it turns `INCREASE_RRSP_DEDUCTION` into "Increase rrsp
   deduction" and `RULES_EVALUATION_FAILED` into "Rules evaluation failed", and
   — worse — it turns any code nobody has ever considered into confident-looking
   prose. A prettifier cannot tell the difference between a term that was
   translated and a term that was merely reformatted, so neither could a
   reviewer.

   THE RULE THIS FILE ENCODES

     Level 1   what it means for me, in one short line, in plain English
     Level 2   why, and what to do about it — deterministic, grounded in the
               state that was actually returned, never generated
     technical the formal or internal term, correctly cased, kept reachable

   A term is never DELETED, only DEMOTED. Every entry below keeps its precise
   name in `technical`, so the audit trail a sophisticated user (or a regulator)
   needs is one disclosure away rather than gone.

   NO FALLBACK, ON PURPOSE. `describe()` returns `null` for anything it does not
   know. It does not guess, and it does not prettify. A caller that receives
   `null` must decide what to do — usually render nothing — because a state
   nobody has written words for is a state nobody has reviewed, and inventing
   tax meaning at render time is exactly what the AI boundary forbids a model
   from doing. The same rule binds a string function.

   EXHAUSTIVENESS IS ENFORCED TWICE. TypeScript requires every member of each
   union below to have an entry, so a mapping cannot be forgotten at compile
   time. And `lexicon.contract.test.ts` reads the BACKEND's own enum and
   registry files and fails when a governed member exists there with no entry
   here — so adding a state to the engine without deciding how to say it to a
   customer breaks the build rather than reaching a customer.
   ========================================================================= */

export interface ConsumerTerm {
  /** One short line: what this means for the person reading it. */
  readonly level1: string
  /** Plain English: what happened, why it matters, what to do. Deterministic. */
  readonly level2: string
  /** The formal or internal term, correctly cased. Never lost, only demoted. */
  readonly technical: string
  /**
   * States that describe Onyx's own architecture rather than the customer's
   * tax. `RESERVED_NO_PRODUCER` tells a person nothing they can act on and
   * everything about our node graph. Marked here so a surface can omit them
   * deliberately, with the technical term still available underneath.
   */
  readonly suppress?: true
}

/* ------------------------------------------------------------------------ */
/* Assurance status — how far Onyx can stand behind one family or item.      */
/* Source of truth: backend/app/services/state_graph/assurance.py            */
/* ------------------------------------------------------------------------ */

export type AssuranceStatus =
  | 'READY'
  | 'EVIDENCE_REQUIRED'
  | 'REVIEW_REQUIRED'
  | 'BLOCKED'
  | 'UNAVAILABLE'
  | 'NOT_APPLICABLE'

export const ASSURANCE_STATUS: Record<AssuranceStatus, ConsumerTerm> = {
  READY: {
    level1: 'Ready',
    level2: 'Onyx has what it needs here and can stand behind this.',
    technical: 'Ready',
  },
  EVIDENCE_REQUIRED: {
    level1: 'We need a document',
    level2:
      'Onyx cannot confirm this without seeing the paperwork behind it. Until then it is not counted.',
    technical: 'Evidence required',
  },
  REVIEW_REQUIRED: {
    level1: 'Needs a look',
    level2: 'Something here needs a decision or a second check before it counts.',
    technical: 'Review required',
  },
  BLOCKED: {
    level1: 'Not possible yet',
    level2: 'Something else has to be sorted out before this can go ahead.',
    technical: 'Blocked',
  },
  UNAVAILABLE: {
    level1: 'Nothing here yet',
    level2:
      'Onyx has not worked this out yet. That is different from there being nothing to find.',
    technical: 'Unavailable — no governing run has produced this',
  },
  NOT_APPLICABLE: {
    level1: 'Does not apply to you',
    level2: 'Nothing in your situation this year touches this.',
    technical: 'Not applicable',
  },
}

/* ------------------------------------------------------------------------ */
/* Action — what the customer can actually do about an item.                */
/* ------------------------------------------------------------------------ */

export type ActionStatus =
  | 'ACTION_AVAILABLE'
  | 'DECISION_REQUIRED'
  | 'EVIDENCE_REQUIRED'
  | 'BLOCKED'

export const ACTION_STATUS: Record<ActionStatus, ConsumerTerm> = {
  ACTION_AVAILABLE: {
    level1: 'Ready when you are',
    level2: 'Nothing is stopping this. You can act on it whenever you like.',
    technical: 'Action available',
  },
  DECISION_REQUIRED: {
    level1: 'We need you to choose',
    level2: 'There is more than one way to handle this, so Onyx will not pick for you.',
    technical: 'Decision required',
  },
  EVIDENCE_REQUIRED: {
    level1: 'We need a document',
    level2: 'Send the paperwork and Onyx can include this.',
    technical: 'Evidence required',
  },
  BLOCKED: {
    level1: 'Not possible yet',
    level2: 'Something earlier has to be resolved before this one can move.',
    technical: 'Blocked',
  },
}

/* ------------------------------------------------------------------------ */
/* Urgency — the deadline axis, kept separate from status by the engine and  */
/* kept separate here for the same reason.                                   */
/* ------------------------------------------------------------------------ */

export type UrgencyStatus = 'NO_DEADLINE' | 'NORMAL' | 'APPROACHING' | 'URGENT' | 'EXPIRED'

export const URGENCY_STATUS: Record<UrgencyStatus, ConsumerTerm> = {
  NO_DEADLINE: {
    level1: 'No deadline',
    level2: 'Nothing about this expires, so there is no rush.',
    technical: 'No deadline',
  },
  NORMAL: {
    level1: 'Plenty of time',
    level2: 'There is a deadline, but it is a long way off.',
    technical: 'Normal',
  },
  APPROACHING: {
    level1: 'Coming up',
    level2: 'The deadline for this is near enough to be worth planning around.',
    technical: 'Approaching',
  },
  URGENT: {
    level1: 'Soon',
    level2: 'The deadline for this is close. Worth doing next.',
    technical: 'Urgent',
  },
  EXPIRED: {
    level1: 'Deadline passed',
    level2: 'The date for this has gone by, so it is no longer available for this tax year.',
    technical: 'Expired',
  },
}

/* ------------------------------------------------------------------------ */
/* Evidence readiness — whether the documents a rule demands are held.       */
/* ------------------------------------------------------------------------ */

export type EvidenceReadiness = 'READY' | 'PARTIAL' | 'MISSING' | 'NOT_REQUIRED' | 'UNKNOWN'

export const EVIDENCE_READINESS: Record<EvidenceReadiness, ConsumerTerm> = {
  READY: {
    level1: 'We have what we need',
    level2: 'Everything this needs is on file.',
    technical: 'Ready',
  },
  PARTIAL: {
    level1: 'We need one more thing',
    level2: 'Some of the paperwork is here. One or more pieces are still missing.',
    technical: 'Partial',
  },
  MISSING: {
    level1: 'We need a document',
    level2: 'None of the paperwork this needs is on file yet.',
    technical: 'Missing',
  },
  NOT_REQUIRED: {
    level1: 'No document needed',
    level2: 'This one does not need anything from you.',
    technical: 'Not required',
  },
  UNKNOWN: {
    level1: "We're still checking",
    level2:
      'Onyx cannot yet tell whether this needs paperwork, because whether it applies to you is still open.',
    technical: 'Unknown',
  },
}

/* ------------------------------------------------------------------------ */
/* Assurance families — the ten node types the engine reasons over. These    */
/* are the domain model, and a first-time filer should not be reading them   */
/* at Level 1 at all. Kept so the advanced view can name them properly.      */
/* ------------------------------------------------------------------------ */

export type AssuranceFamily =
  | 'FACT'
  | 'TAX_STATE'
  | 'OPPORTUNITY'
  | 'DEADLINE'
  | 'EVIDENCE'
  | 'RESOURCE'
  | 'ASSUMPTION'
  | 'SCENARIO'
  | 'OBLIGATION'
  | 'DECISION'

export const ASSURANCE_FAMILY: Record<AssuranceFamily, ConsumerTerm> = {
  FACT: {
    level1: 'What you told us',
    level2: 'The income, expenses and details recorded for this year.',
    technical: 'Fact',
  },
  TAX_STATE: {
    level1: 'Your numbers',
    level2: 'The tax figures Onyx worked out from those details.',
    technical: 'Tax state',
  },
  OPPORTUNITY: {
    level1: 'Things that could help',
    level2: 'Moves Onyx found that might reduce what you owe.',
    technical: 'Opportunity',
  },
  DEADLINE: {
    level1: 'Dates that matter',
    level2: 'Cut-off dates attached to anything Onyx found.',
    technical: 'Deadline',
  },
  EVIDENCE: {
    level1: 'Documents',
    level2: 'The paperwork behind what Onyx has counted.',
    technical: 'Evidence',
  },
  RESOURCE: {
    level1: 'Room you have',
    level2: 'Limits and allowances you have not used up, such as contribution room.',
    technical: 'Resource',
  },
  ASSUMPTION: {
    level1: 'Things we assumed',
    level2: 'Where Onyx had to assume something rather than being told it.',
    technical: 'Assumption',
  },
  SCENARIO: {
    level1: 'What-ifs',
    level2: 'Versions of your year Onyx modelled to compare against the real one.',
    technical: 'Scenario',
  },
  OBLIGATION: {
    level1: 'Things you must do',
    level2: 'Requirements rather than options.',
    technical: 'Obligation',
  },
  DECISION: {
    level1: 'Choices you made',
    level2: 'Decisions recorded against this year.',
    technical: 'Decision',
  },
}

/* ------------------------------------------------------------------------ */
/* Reason codes — WHY a family or item is in the state it is in. These are   */
/* the codes that leaked most visibly: an empty account was told "Reserved   */
/* no producer" on its first screen.                                         */
/* ------------------------------------------------------------------------ */

export type ReasonCode =
  | 'ANALYSIS_RUN_AUTHORITATIVE'
  | 'ASSUMPTION_DECLARATION_ABSENT'
  | 'ELIGIBILITY_INDETERMINATE'
  | 'GOVERNED_RE_EVALUATION_REQUESTED'
  | 'INPUTS_CHANGED_SINCE_EVALUATION'
  | 'LIVE_RECORDS_AUTHORITATIVE'
  | 'NO_ANALYSIS_RUN_FOR_TAX_YEAR'
  | 'NO_OPTIMIZATION_RUN_FOR_TAX_YEAR'
  | 'OPTIMIZATION_RUN_AUTHORITATIVE'
  | 'PORTFOLIO_EXCLUSION'
  | 'RESERVED_NO_PRODUCER'

export const REASON_CODE: Record<ReasonCode, ConsumerTerm> = {
  ANALYSIS_RUN_AUTHORITATIVE: {
    level1: 'From your latest run',
    level2: 'These figures come from the last analysis Onyx ran for this year.',
    technical: 'Analysis run authoritative',
  },
  ASSUMPTION_DECLARATION_ABSENT: {
    level1: 'Nothing assumed here',
    level2: 'Onyx did not have to assume anything for this.',
    technical: 'Assumption declaration absent',
  },
  ELIGIBILITY_INDETERMINATE: {
    level1: "We can't confirm this yet",
    level2: 'One more detail decides whether this applies to you.',
    technical: 'Eligibility indeterminate',
  },
  GOVERNED_RE_EVALUATION_REQUESTED: {
    level1: "We're taking another look",
    level2: 'Onyx is re-checking this. Nothing is needed from you.',
    technical: 'Governed re-evaluation requested',
  },
  INPUTS_CHANGED_SINCE_EVALUATION: {
    level1: 'Something changed',
    level2:
      'Details changed after this was worked out, so the figure may move. Onyx will re-check it.',
    technical: 'Inputs changed since evaluation',
  },
  LIVE_RECORDS_AUTHORITATIVE: {
    level1: 'Up to date',
    level2: 'Read straight from your current records.',
    technical: 'Live records authoritative',
  },
  NO_ANALYSIS_RUN_FOR_TAX_YEAR: {
    level1: "We haven't worked this out yet",
    level2: 'Add your details for this year and Onyx will work out where you stand.',
    technical: 'No analysis run for tax year',
  },
  NO_OPTIMIZATION_RUN_FOR_TAX_YEAR: {
    level1: "We haven't looked at this yet",
    level2: 'Once Onyx has your details it will look for things that could help.',
    technical: 'No optimization run for tax year',
  },
  OPTIMIZATION_RUN_AUTHORITATIVE: {
    level1: 'From your latest run',
    level2: 'These come from the last time Onyx looked for opportunities.',
    technical: 'Optimization run authoritative',
  },
  PORTFOLIO_EXCLUSION: {
    level1: 'Left out of the plan',
    level2:
      'This one was set aside because it clashes with something else Onyx recommended.',
    technical: 'Portfolio exclusion',
  },
  /* Architecture, not tax. Nothing a customer can read, act on, or should
     see — it says a node family exists in the graph with no producer wired to
     it. Suppressed at Level 1; the term survives for the advanced view. */
  RESERVED_NO_PRODUCER: {
    level1: '',
    level2: '',
    technical: 'Reserved — no producer',
    suppress: true,
  },
}

/* ------------------------------------------------------------------------ */
/* Opportunity and lever codes.                                              */
/* Source of truth: backend/app/services/ioe/domain/levers.py, which         */
/* documents itself as "a closed, reviewable allow-list rather than          */
/* something rule authors can extend" — which is what makes an exhaustive    */
/* mapping possible here.                                                     */
/*                                                                            */
/* ACRONYMS ARE WRITTEN, NOT DERIVED. `humanize('INCREASE_RRSP_DEDUCTION')`  */
/* produced "Increase rrsp deduction". RRSP is a proper name; lowercasing it */
/* is not a simplification, it is a spelling mistake in a financial product. */
/* ------------------------------------------------------------------------ */

export type OpportunityCode =
  | 'ADD_EMPLOYMENT_INCOME'
  | 'ADJUST_ELIGIBLE_DIVIDENDS'
  | 'ADJUST_NON_ELIGIBLE_DIVIDENDS'
  | 'CHANGE_PROVINCE'
  | 'DEFER_CAPITAL_GAINS'
  | 'INCREASE_BUSINESS_EXPENSES'
  | 'INCREASE_CHILDCARE'
  | 'INCREASE_DONATIONS'
  | 'INCREASE_FHSA_DEDUCTION'
  | 'INCREASE_MEDICAL_EXPENSES'
  | 'INCREASE_RRSP_DEDUCTION'
  | 'INCREASE_TUITION'
  | 'REALIZE_CAPITAL_GAINS'
  | 'REMOVE_EMPLOYMENT_INCOME'
  | 'RETIRE'

export const OPPORTUNITY_CODE: Record<OpportunityCode, ConsumerTerm> = {
  ADD_EMPLOYMENT_INCOME: {
    level1: 'Earning more from a job',
    level2: 'What your tax would look like with more employment income.',
    technical: 'Add employment income',
  },
  ADJUST_ELIGIBLE_DIVIDENDS: {
    level1: 'Changing your dividend income',
    level2:
      'What changes if the dividends you receive from Canadian companies go up or down.',
    technical: 'Adjust eligible dividends',
  },
  ADJUST_NON_ELIGIBLE_DIVIDENDS: {
    level1: 'Changing your dividend income',
    level2: 'What changes if dividends from a small business go up or down.',
    technical: 'Adjust non-eligible dividends',
  },
  CHANGE_PROVINCE: {
    level1: 'Living somewhere else',
    level2: 'What your tax would look like under another province’s rules.',
    technical: 'Change province',
  },
  DEFER_CAPITAL_GAINS: {
    level1: 'Waiting before you sell',
    level2: 'What changes if a sale happens next year instead of this one.',
    technical: 'Defer capital gains',
  },
  INCREASE_BUSINESS_EXPENSES: {
    level1: 'Claiming more business costs',
    level2: 'What changes if more of your business spending is claimed.',
    technical: 'Increase business expenses',
  },
  INCREASE_CHILDCARE: {
    level1: 'Claiming more child care',
    level2: 'What changes if more of your child care cost is claimed.',
    technical: 'Increase childcare',
  },
  INCREASE_DONATIONS: {
    level1: 'Giving more to charity',
    level2: 'What changes if you donate more before the year ends.',
    technical: 'Increase donations',
  },
  INCREASE_FHSA_DEDUCTION: {
    level1: 'Putting money into an FHSA',
    level2:
      'Money you put into a First Home Savings Account can lower the tax you owe.',
    technical: 'Increase FHSA deduction',
  },
  INCREASE_MEDICAL_EXPENSES: {
    level1: 'Claiming more medical costs',
    level2: 'What changes if more of your medical spending is claimed.',
    technical: 'Increase medical expenses',
  },
  INCREASE_RRSP_DEDUCTION: {
    level1: 'Putting money into an RRSP',
    level2:
      'Money you put into a Registered Retirement Savings Plan can lower the tax you owe.',
    technical: 'Increase RRSP deduction',
  },
  INCREASE_TUITION: {
    level1: 'Claiming your tuition',
    level2: 'Tuition you paid can reduce your tax.',
    technical: 'Increase tuition',
  },
  REALIZE_CAPITAL_GAINS: {
    level1: 'Selling this year instead',
    level2: 'What changes if a sale happens this year rather than later.',
    technical: 'Realize capital gains',
  },
  REMOVE_EMPLOYMENT_INCOME: {
    level1: 'Earning less from a job',
    level2: 'What your tax would look like with less employment income.',
    technical: 'Remove employment income',
  },
  RETIRE: {
    level1: 'Retiring',
    level2: 'What your tax would look like if you stopped working.',
    technical: 'Retire',
  },
}

/* ------------------------------------------------------------------------ */
/* Shared resource codes — the pools and allowances a lever DRAWS ON.        */
/*                                                                            */
/* A separate vocabulary from the levers above, and the exhaustiveness guard  */
/* caught them being conflated on its first run: `shared_resource_code=` and  */
/* `code=` end in the same four characters, and "RRSP room" is a thing you    */
/* HAVE, while "put money into an RRSP" is a thing you DO. Telling a customer */
/* the second when the engine said the first would be a lie the prettifier    */
/* would have told just as readily.                                           */
/* ------------------------------------------------------------------------ */

export type ResourceCode =
  | 'CHILDCARE_POOL'
  | 'DONATION_POOL'
  | 'FHSA_ROOM'
  | 'MEDICAL_POOL'
  | 'RRSP_ROOM'
  | 'TUITION_POOL'

export const RESOURCE_CODE: Record<ResourceCode, ConsumerTerm> = {
  CHILDCARE_POOL: {
    level1: 'Child care you can still claim',
    level2: 'How much child care cost is left to claim this year.',
    technical: 'Childcare pool',
  },
  DONATION_POOL: {
    level1: 'Donations you can still claim',
    level2: 'How much of your giving is left to claim this year.',
    technical: 'Donation pool',
  },
  FHSA_ROOM: {
    level1: 'FHSA room you have left',
    level2: 'How much you can still put into a First Home Savings Account.',
    technical: 'FHSA room',
  },
  MEDICAL_POOL: {
    level1: 'Medical costs you can still claim',
    level2: 'How much medical spending is left to claim this year.',
    technical: 'Medical pool',
  },
  RRSP_ROOM: {
    level1: 'RRSP room you have left',
    level2: 'How much you can still put into a Registered Retirement Savings Plan.',
    technical: 'RRSP room',
  },
  TUITION_POOL: {
    level1: 'Tuition you can still claim',
    level2: 'How much tuition is left to claim this year.',
    technical: 'Tuition pool',
  },
}

/* ------------------------------------------------------------------------ */
/* WHAT THIS LEXICON DOES NOT YET COVER                                      */
/* ------------------------------------------------------------------------ */
/* Written down rather than left to be discovered, because a translation      */
/* layer that silently covers half a product is worse than one whose edges    */
/* are known.                                                                 */
/*                                                                            */
/* Covered here: the assurance path — family, status, action, urgency,        */
/* evidence readiness, reason codes — plus the lever and shared-resource      */
/* registries. That is what reaches the first signed-in screen and the        */
/* opportunity surface, which is where the audit found the leak worst.        */
/*                                                                            */
/* NOT covered yet, and still rendered through `humanize()` at the call site: */
/*                                                                            */
/*   opportunity LIFECYCLE   availability, decision, execution, actionability */
/*                           (app/services/ioe/domain/enums.py — 24 further   */
/*                           StrEnums, several consumer-reachable)            */
/*   deadline codes          `deadline_code`                                  */
/*   document type codes     `document_type_code`                             */
/*   scenario + comparison   DecisionTwin, BeforeYouAct, WhatChanged          */
/*                                                                            */
/* Those are a following entry. They are named here so the next person does   */
/* not have to rediscover the boundary, and so nobody mistakes a partial      */
/* lexicon for a complete one.                                                */

/* ------------------------------------------------------------------------ */
/* Access                                                                    */
/* ------------------------------------------------------------------------ */

/** The vocabularies a consumer surface may translate. */
export const LEXICON = {
  assuranceStatus: ASSURANCE_STATUS,
  action: ACTION_STATUS,
  urgency: URGENCY_STATUS,
  evidenceReadiness: EVIDENCE_READINESS,
  family: ASSURANCE_FAMILY,
  reason: REASON_CODE,
  opportunity: OPPORTUNITY_CODE,
  resource: RESOURCE_CODE,
} as const

export type Vocabulary = keyof typeof LEXICON

/**
 * Look up one governed code.
 *
 * RETURNS `null` RATHER THAN A GUESS. There is deliberately no fallback that
 * reformats an unrecognised code into something that reads like English: a
 * state nobody has written words for is a state nobody has reviewed, and a
 * financial product that invents a plausible sentence about an unreviewed tax
 * state is doing the thing the AI boundary exists to prevent. Callers handle
 * `null` explicitly — normally by rendering nothing.
 */
export function describe(vocabulary: Vocabulary, code: string | null | undefined): ConsumerTerm | null {
  if (!code) return null
  const table: Record<string, ConsumerTerm> = LEXICON[vocabulary]
  return table[code] ?? null
}

/** Level 1, or `null`. Empty for suppressed states, which have nothing to say. */
export function level1(vocabulary: Vocabulary, code: string | null | undefined): string | null {
  const term = describe(vocabulary, code)
  if (!term || term.suppress) return null
  return term.level1
}

/** Level 2, or `null`. */
export function level2(vocabulary: Vocabulary, code: string | null | undefined): string | null {
  const term = describe(vocabulary, code)
  if (!term || term.suppress) return null
  return term.level2
}

/**
 * The formal term, kept reachable.
 *
 * Available even for suppressed states: a customer never needs to read
 * "Reserved — no producer", and somebody auditing the same screen does.
 */
export function technical(vocabulary: Vocabulary, code: string | null | undefined): string | null {
  return describe(vocabulary, code)?.technical ?? null
}
