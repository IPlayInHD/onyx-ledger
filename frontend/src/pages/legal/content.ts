/* =========================================================================
   LEGAL AND POLICY CONTENT
   =========================================================================
   The six documents Onyx Ledger publishes, as data rather than as markup.
   Keeping them here means the renderer is one small component, the reading
   order of a document is visible in one place, and nothing arrives as HTML —
   there is no path by which policy copy could be injected into the page.

   THESE ARE DRAFTS AND THEY SAY SO. Nothing below has been reviewed by
   Canadian legal counsel. Every document therefore states the position the
   product can actually defend today: what the software does, what it refuses
   to do, what is built, and — where a decision has not been made — that it has
   not been made. A placeholder is written as a placeholder. The alternative,
   a confident sentence covering an unmade decision, is the failure mode this
   file exists to avoid.

   The claims here were written against the backend as it stands: Argon2id
   password hashing, PostgreSQL row-level security keyed to the account,
   separate rate / concurrency / platform admission limits, credential values
   replaced with a marker before audit rows are written, the account-deletion
   endpoint, the retention registry, and an explanation path that sends nothing
   to a third-party model provider. If one of those changes, the sentence about
   it here is now wrong and must change with it.
   ========================================================================= */

export interface LegalSection {
  heading: string
  paragraphs?: string[]
  bullets?: string[]
}

export interface LegalDoc {
  id: string
  title: string
  summary: string
  version: string
  effectiveDate: string
  lastUpdated: string
  sections: LegalSection[]
}

/* One version and one pair of dates across the whole set. The six documents
   were drafted together and are reviewed together; letting them drift apart
   would imply a review history that did not happen. */
const DOC_VERSION = '0.1.0-draft'
const DOC_EFFECTIVE = '2026-08-21'
const DOC_UPDATED = '2026-08-21'

function draft(
  doc: Omit<LegalDoc, 'version' | 'effectiveDate' | 'lastUpdated'>,
): LegalDoc {
  return {
    ...doc,
    version: DOC_VERSION,
    effectiveDate: DOC_EFFECTIVE,
    lastUpdated: DOC_UPDATED,
  }
}

/* ----------------------------------------------------------------- terms -- */

const TERMS = draft({
  id: 'terms',
  title: 'Terms of Service',
  summary:
    'The agreement covering your use of Onyx Ledger: what the service does, what it asks of you, and the limits on what it promises.',
  sections: [
    {
      heading: 'Who these terms are between',
      paragraphs: [
        'These terms are an agreement between you and the operator of Onyx Ledger. They apply whenever you create an account, sign in, or use any part of the service.',
        'The operating entity is not named in this draft. Naming it — and confirming that the entity described is the one that carries these obligations — is one of the items reserved for Canadian legal counsel at the end of this document.',
        'By creating an account you accept these terms. If you do not accept them, do not create an account.',
      ],
    },
    {
      heading: 'What Onyx Ledger is',
      paragraphs: [
        'Onyx Ledger is software that produces Canadian personal tax information and estimates. It reads facts you enter, applies published tax rules that have been recorded in versioned and governed form, and reports what its engine calculates: an estimated tax position, provisions its rules say may apply to your circumstances, and the modelled effect of a decision you are weighing.',
        'Everything the service produces is software-generated information. It is an estimate of your position. It is not a determination of your tax, and it is not a filing.',
      ],
      bullets: [
        'Onyx Ledger does not file returns, forms, elections or payments with the Canada Revenue Agency or with any provincial or territorial tax authority.',
        'Onyx Ledger does not make contributions, transfers or claims on your behalf. Every action stays yours to take.',
        'Onyx Ledger is not an accounting firm, a law firm or a registered tax preparer, and nobody reviews your file individually.',
        'Coverage is limited to the tax years and the provinces the service states it supports. A year or a jurisdiction outside that set is not modelled.',
      ],
    },
    {
      heading: 'Your account',
      paragraphs: [
        'You need an account to use the product, and you must be old enough to enter into a contract where you live.',
      ],
      bullets: [
        'Give accurate information. The engine calculates from what you enter, and an inaccurate fact produces an inaccurate estimate that the software has no way to detect.',
        'Keep your password to yourself, and use one you do not use anywhere else. You are responsible for what happens under your account.',
        'An account is for one person. Do not share sign-in credentials.',
        'Tell us promptly if you believe someone else has used your account.',
      ],
    },
    {
      heading: 'Acceptable use',
      paragraphs: [
        'The limits below exist to keep the service available and to keep other people’s financial information safe.',
      ],
      bullets: [
        'Do not attempt to reach an account, document or record that is not yours.',
        'Do not probe, scan or test the security of the service, and do not attempt to work around its rate, concurrency or size limits.',
        'Do not scrape the service, extract its content in bulk, or use automated tools to create accounts.',
        'Do not decompile or reverse engineer the service, except where that restriction is unenforceable under applicable law.',
        'Do not upload malicious files, or any material you do not have the right to provide.',
        'Do not present output from Onyx Ledger to another person as professional tax advice, whether yours or ours.',
        'Do not use the service to build a competing product or to assemble a data set from its output.',
      ],
    },
    {
      heading: 'No professional-advice relationship',
      paragraphs: [
        'Using Onyx Ledger does not create an accountant-client, solicitor-client, agency or fiduciary relationship between you and us. We do not review your circumstances, exercise professional judgment about them, or accept an engagement to advise you.',
        'The service reports what its governed rules and its engine produce from the facts you entered. Whether that result is right for your situation is a professional question, and the Tax Information Disclaimer sets out what that means in practice.',
      ],
    },
    {
      heading: 'Your information',
      paragraphs: [
        'What Onyx Ledger collects, why, how long it is kept and how to have it deleted is described in the Privacy Policy, which forms part of these terms.',
        'The security controls that are actually in place, and the ones that are still being built, are described on the Security page. It is written to be read before you decide to trust the service with a financial fact, not after.',
      ],
    },
    {
      heading: 'The service will change',
      paragraphs: [
        'Onyx Ledger is pre-release software under active development. Features may be added, changed or withdrawn. Governed tax data is republished as its sources change, and a result built on a superseded source version is marked rather than quietly rewritten.',
        'This draft offers no availability or uptime commitment. The service may be unavailable for maintenance, or for reasons outside our control.',
        'If these terms change, the version and the effective date at the top of this document change with them. A change that materially affects your rights should be notified rather than published silently; the form that notice takes is one of the items reserved for counsel below.',
      ],
    },
    {
      heading: 'Limitation of liability',
      paragraphs: [
        'To the fullest extent permitted by applicable law, the service is provided as it is and as available, without warranties of any kind, including any implied warranty of merchantability, fitness for a particular purpose, accuracy, or uninterrupted operation.',
        'To the fullest extent permitted by applicable law, we are not liable for indirect, incidental, special or consequential loss, for lost profits or lost savings, or for interest, penalties or reassessments imposed by a tax authority, arising out of your use of the service or your reliance on anything it produced.',
        'Nothing in these terms excludes or limits liability that cannot be excluded or limited by law. Consumer protection legislation in several Canadian provinces gives you rights that an agreement cannot take away, and these terms do not attempt to take them away.',
        'A monetary cap on liability is only meaningful if it is enforceable in the province whose law applies. This draft deliberately states none, for the reason given in the next section.',
      ],
    },
    {
      heading: 'Ending your use of the service',
      paragraphs: [
        'You may stop using Onyx Ledger at any time, and you may ask for your account to be deleted from the settings screen inside the product. What deletion does is described in the Privacy Policy.',
        'We may suspend or end access to an account that breaches the acceptable-use section, where we are required to by applicable law, or where continuing to serve it would put the platform or other customers at risk. Where the reason and the circumstances allow, we will say why.',
        'The parts of this agreement that by their nature outlive it — the disclaimers, the limitation of liability, and anything concerning information already collected — continue after it ends.',
      ],
    },
    {
      heading: 'Governing law — placeholder requiring counsel',
      paragraphs: [
        'This draft states no governing law, no forum for disputes and no dispute-resolution procedure. These are not ordinary blanks. The choice affects which consumer protection statutes apply to you, whether an arbitration or class-action term would be enforceable at all, and which court could hear a claim.',
        'Writing that choice into a document nobody qualified has reviewed would create the appearance of a settled position where none exists. It is left open deliberately, and it must be completed by qualified Canadian counsel before this service is offered to the public.',
      ],
    },
    {
      heading: 'Contact',
      paragraphs: [
        'An address for legal notices has not been published yet. This is a placeholder, and it must be completed before these terms are relied on.',
        'Until it is, treat this document as a draft prepared for review rather than as a live agreement.',
      ],
    },
  ],
})

/* --------------------------------------------------------------- privacy -- */

const PRIVACY = draft({
  id: 'privacy',
  title: 'Privacy Policy',
  summary:
    'What Onyx Ledger collects about you, why it collects it, how long it is kept, and how to have it deleted.',
  sections: [
    {
      heading: 'Scope',
      paragraphs: [
        'This policy describes how Onyx Ledger handles personal information about the people who use it.',
        'It is written against the Personal Information Protection and Electronic Documents Act (PIPEDA), the federal Canadian privacy statute covering personal information handled in the course of commercial activity, and against its principles: identify the purposes, limit collection to them, be open about how information is handled, and let people see what is held about them.',
        'Whether provincial privacy legislation also applies, and how, depends on where the operating entity and its customers are located. That determination is reserved for counsel.',
      ],
    },
    {
      heading: 'What Onyx Ledger collects',
      bullets: [
        'Your email address. It identifies the account, and the service uses it to send you a link confirming the address, a link if you ask to reset your password, and a notice when your password changes. It is not used for anything else, and no message the service sends contains tax information.',
        'Your password, stored only as an Argon2id hash. The password itself is never written to the database, to logs, or to audit records.',
        'The tax facts you enter: income amounts and their kinds, registered-account contributions and balances you record, expenses, your province of residence, and circumstances such as marital status and dependants where the rules turn on them.',
        'Documents you upload, the fields extracted from them, and your confirmation of those fields. Files are held in object storage; their contents are not copied into the application database.',
        'Results derived from those facts: analyses, scenarios, opportunity findings, evidence status, and the record of which governed source versions each result used.',
        'Operational and security records: sign-in times, the network address associated with a security event, and an append-only record of changes made to your data.',
      ],
    },
    {
      heading: 'Why it is collected',
      paragraphs: [
        'Each category above exists for a purpose that can be stated plainly. The service does not collect information for purposes it has not identified.',
      ],
      bullets: [
        'To operate your account and let you sign in.',
        'To calculate the estimates and findings you asked for.',
        'To keep a record of what a result was based on, so a figure you acted on can be explained later rather than only recalculated.',
        'To protect the platform: to detect abuse, to enforce rate and concurrency limits, and to investigate a security event.',
      ],
    },
    {
      heading: 'No advertising, and no sale of personal information',
      paragraphs: [
        'Onyx Ledger does not sell personal information, and there is no arrangement under which a third party receives your information in exchange for anything of value.',
        'Your tax facts are not used for advertising, are not used to build a profile for anyone else, and are not shared with data brokers. The product carries no advertising and no third-party tracking of the kind that would make that possible.',
      ],
    },
    {
      heading: 'Consent',
      paragraphs: [
        'You give consent by creating an account and entering information, for the purposes described above.',
        'You can withdraw it by asking for your account to be deleted. The service cannot produce an estimate without the facts it calculates from, so withdrawing consent and continuing to use the product are not compatible, and this policy will not pretend otherwise.',
        'If Onyx Ledger ever wants to use your information for a purpose not listed here, it needs consent for that purpose first, and this policy has to say so before the change takes effect.',
      ],
    },
    {
      heading: 'How your information is kept separate',
      paragraphs: [
        'Each account’s data is isolated in the database itself. PostgreSQL row-level security is keyed to the account that made the request, so a query that failed to filter by account still cannot return another account’s rows. The isolation does not depend on every line of application code remembering to ask the right question.',
        'Uploaded documents are stored under keys tied to the account that uploaded them, and are never held in the application database.',
      ],
    },
    {
      heading: 'How long it is kept',
      paragraphs: [
        'Retention is governed by a registry that names, for every table holding data derived from you, which class of retention applies to it and what account deletion does to it. A new table cannot reach production without that decision being recorded.',
        'The classes are: kept while the account is active; tied to how long a tax year stays relevant; short operational data measured in hours or days; and a bounded window for security-audit records.',
        'The exact durations behind two of those classes — the tax-year window and the audit window — have not been set. Both depend on record-keeping obligations and on legal advice this draft has not had, and the service records them as explicitly pending rather than quietly keeping data forever. Setting them is required before production.',
      ],
    },
    {
      heading: 'Your rights, and how to exercise them',
      paragraphs: [
        'PIPEDA gives you the right to know what an organisation holds about you, to ask for it to be corrected, and to challenge how it is handled. In this product that works as follows.',
      ],
      bullets: [
        'Access and correction, in the product. The facts Onyx Ledger holds about your tax position are the facts you entered, and each is visible and editable on the screen where it was entered: your profile, your income, your registered accounts, your documents.',
        'Deletion, in the product. Settings and privacy carries a deletion request for your own account. Access ends when the request is accepted, and a purge governed by the retention registry follows.',
        'A complete copy of everything held. Onyx Ledger does not yet offer a self-serve export of everything it holds about you. Until it does, an access request has to go to the contact address below.',
      ],
    },
    {
      heading: 'Service providers',
      paragraphs: [
        'Running the service requires infrastructure — hosting, a managed database, object storage — and a provider of infrastructure necessarily processes what is stored on it.',
        'The production deployment has not been finalised, so this draft names no provider. Naming them, and stating the countries their infrastructure operates in, is required before this policy is published.',
        'One thing can be stated now: no third-party artificial-intelligence provider currently receives customer data. Explanations are produced inside the service. If that changes, this policy and the AI Transparency Statement have to be updated before the change takes effect, not after it.',
      ],
    },
    {
      heading: 'Where your information is held',
      paragraphs: [
        'The location of storage and processing follows the hosting decision described above, and cannot be stated honestly until that decision is made.',
        'Information held outside Canada may be reachable by the authorities of the country it is held in. That is a fact a customer is entitled to know before it becomes true for them, so this section must be completed before production.',
      ],
    },
    {
      heading: 'Children',
      paragraphs: [
        'Onyx Ledger is built for adults filing Canadian personal tax and is not directed at children. It does not knowingly collect personal information from a child. If you believe a child has created an account, use the contact below and it will be removed.',
      ],
    },
    {
      heading: 'What this policy does not claim',
      paragraphs: [
        'No certification, accreditation or third-party privacy audit is claimed anywhere in this document. None has been sought or obtained.',
        'This policy describes handling that is in place today and marks the rest as pending. It is not a compliance attestation, and it has not been reviewed by counsel.',
      ],
    },
    {
      heading: 'Concerns and contact',
      paragraphs: [
        'A privacy contact address has not been published yet. This is a placeholder, and it must be completed before this policy is relied on.',
        'If you have raised a privacy concern with an organisation and are not satisfied with the answer, you can take it to the Office of the Privacy Commissioner of Canada, which oversees PIPEDA.',
      ],
    },
    {
      heading: 'Changes to this policy',
      paragraphs: [
        'When this policy changes, the version and the effective date at the top change with it. A change that materially affects how your information is handled should reach you before it takes effect rather than after.',
      ],
    },
  ],
})

/* ------------------------------------------------------- ai transparency -- */

const AI_TRANSPARENCY = draft({
  id: 'ai-transparency',
  title: 'AI Transparency Statement',
  summary:
    'How artificial intelligence is used at Onyx Ledger, the boundary it is not allowed to cross, and who is responsible for the result.',
  sections: [
    {
      heading: 'The statement',
      paragraphs: [
        'Onyx Ledger was developed with extensive use of AI-assisted software-development tools. Certain customer-facing explanations may also be generated using artificial intelligence.',
        'Generative AI does not determine your tax calculations, eligibility, governed tax amounts, source citations, or scenario arithmetic. Those results are produced by Onyx Ledger deterministic and governed systems.',
        'AI-generated explanations are subject to validation and deterministic fallback controls.',
      ],
    },
    {
      heading: 'Where the authority sits',
      paragraphs: [
        'The division above is the whole point of this statement. A deterministic engine calculates every amount from your recorded facts and from governed tax data that carries the version it was published under. A language model, where one is used, is handed figures that have already been calculated and asked to put them into sentences.',
        'Before a generated explanation reaches you it is checked against the figures it describes. An explanation that fails that check is not shown, and a deterministic explanation is shown in its place. The prose is the part that can be regenerated. The numbers are not.',
      ],
    },
    {
      heading: 'What AI is never allowed to do',
      bullets: [
        'It does not set, change or round any amount.',
        'It does not decide whether a provision applies to you.',
        'It does not choose which governed source version a result uses.',
        'It does not produce citations. A citation comes from the governed source record.',
        'It does not perform scenario arithmetic, or any other arithmetic you are shown.',
        'It does not file, submit or send anything anywhere.',
      ],
    },
    {
      heading: 'Where you will see it',
      paragraphs: [
        'Text written by a language model is labelled wherever it appears, with the same label on every screen, and it is set apart from calculated figures rather than mixed in among them.',
        'You should never have to work out whether you are reading a calculation or a description of one. If a sentence is not marked as generated, a deterministic part of the system wrote it.',
      ],
    },
    {
      heading: 'As currently configured',
      paragraphs: [
        'Today, explanations are produced by a deterministic in-house explainer that grounds its wording in the governed rule text retrieved for your question, and that does not emit dollar figures at all. No customer data is sent to a third-party model provider.',
        'The service is built so that a commercial model could be placed behind the same boundary later. If that happens, this statement and the Privacy Policy have to say so before it takes effect.',
      ],
    },
    {
      heading: 'Who is responsible',
      paragraphs: [
        'Responsibility for operating this service rests with the operator of Onyx Ledger. It does not rest with the AI tools used to build it, and it is not shared with them.',
        'Using AI assistance to write software does not move accountability for what that software does. A defect written with a tool’s help is our defect. An explanation that misleads you is our explanation.',
      ],
    },
    {
      heading: 'Why this statement exists',
      paragraphs: [
        'This statement is published voluntarily. It is not offered in response to an obligation the operator has identified, and no claim is made here that any statute or regulator compels it.',
        'It exists because someone reading a figure is entitled to know what produced the sentence beside it, and because a product that quietly generated its explanations would be asking for trust it had not earned.',
      ],
    },
    {
      heading: 'The limits of generated text',
      paragraphs: [
        'A generated explanation can be vague, or can emphasise the wrong part of a correct answer, even when every figure in it is right. Validation catches contradictions with the underlying figures; it does not make prose insightful.',
        'Where your reading of the words and your reading of the numbers disagree, the figures and their provenance labels are what the service stands behind. If an explanation seems wrong, report it — that is a defect worth knowing about.',
      ],
    },
  ],
})

/* -------------------------------------------------------- tax disclaimer -- */

const TAX_DISCLAIMER = draft({
  id: 'tax-disclaimer',
  title: 'Tax Information Disclaimer',
  summary:
    'Onyx Ledger produces software-generated estimates. This page states plainly what that is, and what it is not.',
  sections: [
    {
      heading: 'Software-generated information',
      paragraphs: [
        'Everything Onyx Ledger shows you — an estimated position, a marginal rate, an opportunity, a scenario result — is information produced by software from the facts you entered and from published tax rules recorded in governed, versioned form.',
        'It is an estimate. It is not a determination of your tax, it is not a filing, and it has not been reviewed by a tax authority or by a professional acting for you.',
      ],
    },
    {
      heading: 'Not a substitute for professional advice',
      paragraphs: [
        'Onyx Ledger is not a substitute for individualised professional tax or legal advice. It does not know your full circumstances, it does not exercise professional judgment about them, and nobody reviews your file.',
        'Onyx Ledger is not an accounting firm, a law firm or a registered tax preparer, and using it does not create an accountant-client or solicitor-client relationship.',
        'Before you act on anything consequential, take the figures — and the workings behind them, which the product shows you — to a qualified professional.',
      ],
    },
    {
      heading: 'The result depends on what you entered',
      paragraphs: [
        'The engine calculates from the facts it has. An amount entered in the wrong place, an income source not recorded, a change of province left unchanged: each produces an estimate that is internally consistent and wrong about you, and the software has no way to detect that.',
        'Review your recorded facts before relying on a result built from them.',
      ],
    },
    {
      heading: 'Assumptions must be confirmed before you act',
      paragraphs: [
        'Where a value is needed and you have not supplied one, the service may use a platform assumption so that a model can run at all. An assumption is labelled as an assumption wherever it appears, and it is never described as known, confirmed or entered by you.',
        'An estimate that rests on an assumption is only as good as that assumption. Confirm it before acting on the result.',
      ],
    },
    {
      heading: 'Scenarios are models, not predictions',
      paragraphs: [
        'A scenario shows what the engine calculates would follow from the facts and assumptions you stated. It is a model of a decision, not a forecast of your year.',
        'Nothing about a modelled result is a promise of an outcome. Circumstances change, and the law in force when you act is the law that governs — not the version the model used.',
      ],
    },
    {
      heading: 'Rules change, and they apply to circumstances',
      paragraphs: [
        'Rates, limits and thresholds come from published sources recorded with the version they were published under, and every result records the version it used. When a source is republished, results built on the older version are marked rather than rewritten silently.',
        'Even so, tax legislation, administrative positions and court decisions change, and a rule that generally applies may not apply to your particular circumstances.',
      ],
    },
    {
      heading: 'Filing and payment remain yours',
      bullets: [
        'Onyx Ledger does not file returns, forms or elections.',
        'Onyx Ledger does not pay, remit or contribute anything on your behalf.',
        'Deadlines are yours to meet. A date shown in the product is presented for information and does not extend or replace the deadline that applies to you.',
        'Interest, penalties and reassessments imposed by a tax authority remain your responsibility.',
      ],
    },
    {
      heading: 'No affiliation with the Canada Revenue Agency',
      paragraphs: [
        'Onyx Ledger is not affiliated with, not endorsed by, not sponsored by and not acting on behalf of the Canada Revenue Agency, Revenu Québec, or any provincial or territorial tax authority.',
        'Nothing the service produces has been reviewed, certified or accepted by any of them, and no communication from Onyx Ledger should be read as coming from a tax authority.',
      ],
    },
  ],
})

/* --------------------------------------------------------- accessibility -- */

const ACCESSIBILITY = draft({
  id: 'accessibility',
  title: 'Accessibility',
  summary:
    'The standard Onyx Ledger is built to, what is implemented today, what is not, and how to report a barrier.',
  sections: [
    {
      heading: 'The standard we build to',
      paragraphs: [
        'Onyx Ledger is designed and reviewed against the Web Content Accessibility Guidelines, version 2.2, at Level AA.',
        'That is a target, not a verified conformance claim. No independent accessibility audit has been carried out, and this page will keep saying so until one has.',
      ],
    },
    {
      heading: 'What is implemented',
      bullets: [
        'Every function can be operated from the keyboard alone, in a sensible order, without a trap.',
        'Focus is always visible. The focus indicator is never removed, only replaced with a more visible one.',
        'Pages use real structure: landmarks, one first-level heading per page, headings in order, lists that are lists and tables that are tables.',
        'Status is never carried by colour alone. A state that matters is written as a word, and usually also as a shape or a stroke.',
        'Visual surfaces have text equivalents. The assurance ledger, for example, is paired with a table carrying the same facts for anyone reading with assistive technology.',
        'Form fields have labels tied to them, errors are linked to the field they belong to, and a form with errors offers a summary at the top.',
        'Interactive controls meet a minimum target size of 44 by 44 CSS pixels.',
        'Motion respects the reduced-motion setting in your operating system. Nothing is left mid-animation, and nothing depends on movement to be understood.',
        'Content reflows to a narrow screen without the page scrolling sideways. Wide tables scroll inside their own container instead.',
        'Colours come from a single token palette chosen against the Level AA contrast minimum, in both the light and the dark theme.',
      ],
    },
    {
      heading: 'Known limitations',
      paragraphs: [
        'Stated plainly, because an accessibility statement that lists only successes is of no use to the person who hit the problem.',
      ],
      bullets: [
        'No independent audit. Automated checks run against the public pages as part of the build, and automated checks find only a fraction of what matters.',
        'Testing with assistive technology has been limited. The product has not been exercised across the full range of screen readers, magnifiers and voice-control software people actually use.',
        'Dense financial tables still need horizontal scrolling inside their container on a small screen. The alternative, hiding columns, would hide figures, which is worse.',
        'Documents you upload are your own files. Onyx Ledger does not remediate them, and a scanned image of a slip remains an image.',
        'Printed and PDF output has not been reviewed for accessibility.',
        'This page is itself a draft, and the target it states has not been independently confirmed.',
      ],
    },
    {
      heading: 'Scope of this statement',
      paragraphs: [
        'This statement covers the Onyx Ledger customer web application. It does not cover files you upload, or content hosted elsewhere and linked to from the product.',
      ],
    },
    {
      heading: 'Reporting a barrier',
      paragraphs: [
        'If something here stopped you, that is worth reporting, and a report about a specific page is worth far more than a general one. Please include:',
      ],
      bullets: [
        'The address of the page you were on.',
        'What you were trying to do, and what happened instead.',
        'The browser and operating system you were using, and any assistive technology.',
      ],
    },
    {
      heading: 'Contact for accessibility reports',
      paragraphs: [
        'An address for accessibility reports has not been published yet. This is a placeholder, and it must be completed before production together with the response time the operator is prepared to commit to.',
        'Publishing a promise about how quickly a barrier will be fixed, before anyone has decided what that promise is, would be worse than publishing nothing.',
      ],
    },
  ],
})

/* -------------------------------------------------------------- security -- */

const SECURITY = draft({
  id: 'security',
  title: 'Security',
  summary:
    'The controls that are in place today, the ones that are still being built, and what Onyx Ledger does not claim.',
  sections: [
    {
      heading: 'How to read this page',
      paragraphs: [
        'This page describes controls that exist in the running service. Where something is planned rather than built, it appears under what is still being built rather than being written in the present tense.',
        'No service can be made perfectly secure, and nothing here says otherwise. What follows is a description, not a promise.',
      ],
    },
    {
      heading: 'Controls in place',
      bullets: [
        'Per-account isolation enforced by the database. PostgreSQL row-level security is keyed to the account making the request, so a query that forgot to filter by account still cannot return another account’s rows. Isolation does not rely on every code path remembering to ask correctly.',
        'Modern password hashing. Passwords are stored as Argon2id hashes, and never in a form that can be read back.',
        'Admission control on three separate axes. How often an operation may be attempted, how many may be in flight at once for one account, and how much the platform as a whole may be doing are limited independently — so one caller cannot monopolise the service, and sign-in cannot be ground at cheaply.',
        'Credential scrubbing in audit records. The change record captures what changed and who changed it; named credential columns have their values replaced with a marker before the record is written, so a password hash is not copied into an append-only table.',
        'An append-only change record. Changes to your data are recorded with the actor, the action and the time, so what happened to a figure can be answered later rather than reconstructed.',
        'Uploads bounded where the bytes land. A document upload is authorised with a size ceiling recorded against it, and the store refuses an object larger than that ceiling. File contents never enter the application database.',
        'No third-party AI provider receives customer data. Explanations are produced inside the service; nothing is sent to an external model provider.',
        'Account deletion that is governed rather than improvised. A deletion request ends access and schedules a purge driven by a registry that declares, for every table holding data derived from you, what deletion does to it.',
      ],
    },
    {
      heading: 'Known gaps in what is already built',
      paragraphs: [
        'Credential scrubbing was added after the defect it fixes was found. Audit records written before that control existed still contain the values they captured, and clearing them requires a privileged erasure path. That is a known open item rather than a resolved one, and it is listed here rather than left for someone to discover.',
      ],
    },
    {
      heading: 'What is still being built',
      paragraphs: [
        'Production hardening is under way and is not finished. Naming what is missing is more useful to you than a page implying everything is done.',
      ],
      bullets: [
        'Edge protection: a web application firewall and denial-of-service mitigation in front of the service.',
        'Formal monitoring and alerting, with someone on the other end of an alert.',
        'Independent security testing: a penetration test and an external review of the code, neither of which has been carried out.',
        'A published incident-response procedure, including how and when you would be told about a breach affecting your information.',
        'Backup and restore testing on a schedule, proven by restoring rather than by assuming.',
      ],
    },
    {
      heading: 'What Onyx Ledger does not claim',
      bullets: [
        'No certification. Onyx Ledger holds no SOC 2 report, no ISO 27001 certificate and no equivalent attestation. None has been sought.',
        'No independent audit or penetration test has been completed.',
        'No claim that the service cannot be compromised. Any service can be, and a page saying otherwise would be asserting something nobody is in a position to know.',
      ],
    },
    {
      heading: 'What you can do',
      bullets: [
        'Use a password unique to Onyx Ledger.',
        'Keep the email address on the account current — it is how the service reaches you about a security matter, and how you recover access if you forget your password.',
        'Sign out on a device you share with anyone else.',
        'Report anything that looks wrong, including anything you can see that you believe you should not be able to.',
      ],
    },
    {
      heading: 'Reporting a vulnerability',
      paragraphs: [
        'If you find a weakness, please report it rather than testing how far it goes. There is no bug-bounty programme, and no formal safe-harbour statement has been published — writing one that means anything requires legal advice this draft has not had.',
        'An address for security reports has not been published yet. This is a placeholder, and it must be completed before production.',
      ],
    },
  ],
})

/* ----------------------------------------------------------------- index -- */

export const LEGAL_DOCS: Record<string, LegalDoc> = {
  [TERMS.id]: TERMS,
  [PRIVACY.id]: PRIVACY,
  [AI_TRANSPARENCY.id]: AI_TRANSPARENCY,
  [TAX_DISCLAIMER.id]: TAX_DISCLAIMER,
  [ACCESSIBILITY.id]: ACCESSIBILITY,
  [SECURITY.id]: SECURITY,
}

/* Reading order, not alphabetical order: the agreement first, then what is
   done with your information, then the three statements that qualify what the
   product's output means. */
export const LEGAL_ORDER: string[] = [
  TERMS.id,
  PRIVACY.id,
  AI_TRANSPARENCY.id,
  TAX_DISCLAIMER.id,
  ACCESSIBILITY.id,
  SECURITY.id,
]
