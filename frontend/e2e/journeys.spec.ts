/* =========================================================================
   END-TO-END JOURNEYS + VALUE PARITY
   =========================================================================
   The central claim of this frontend is that it DISPLAYS the certified
   backend's figures rather than deriving its own. These tests prove it the
   only way that counts: seed a persona through the real API, record what the
   engine actually returned, then read the number off the rendered page and
   require them to be the same value.

   A mocked API would only prove the mock agrees with itself.
   ========================================================================= */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { legalStateAfterVerification } from './account'
import { fillField } from './form'
import { acceptOutstandingLegal } from './legal'
import { tokenFrom, waitForMessage } from './mailbox'

const API = process.env.ONYX_E2E_API ?? 'http://127.0.0.1:8099'
const PASSWORD = 'supersecret1'
const TAX_YEAR = 2025

interface Persona {
  key: string
  description: string
  incomes: { code: string; amount: string }[]
  expenses?: { code: string; amount: string }[]
  registered?: { type: 'RRSP' | 'FHSA'; contributed: string }[]
}

/** The launch personas, mirroring the backend's own acceptance set. The point
 *  is coverage of DIFFERENT tax shapes — CPP bands, donation tiers, mixed
 *  income — so parity is proven across the engine's branches, not just one. */
const PERSONAS: Persona[] = [
  { key: 'employee', description: 'ON employee 60k', incomes: [{ code: 'employment', amount: '60000' }] },
  {
    key: 'employee-medical',
    description: 'ON employee 60k with medical expenses',
    incomes: [{ code: 'employment', amount: '60000' }],
    expenses: [{ code: 'medical', amount: '3000' }],
  },
  {
    key: 'donations',
    description: 'ON employee 90k with donations',
    incomes: [{ code: 'employment', amount: '90000' }],
    expenses: [{ code: 'donation', amount: '10000' }],
  },
  {
    key: 'high-income-donation',
    description: 'high income 310k with 50k donations (33% tier)',
    incomes: [{ code: 'employment', amount: '310000' }],
    expenses: [{ code: 'donation', amount: '50000' }],
  },
  { key: 'se-below-cpp2', description: 'self-employed 50k', incomes: [{ code: 'self_employment', amount: '50000' }] },
  { key: 'se-in-cpp2', description: 'self-employed 75k (inside CPP2 band)', incomes: [{ code: 'self_employment', amount: '75000' }] },
  { key: 'se-above-yampe', description: 'self-employed 90k (above YAMPE)', incomes: [{ code: 'self_employment', amount: '90000' }] },
  {
    key: 'mixed',
    description: 'mixed employment and self-employment',
    incomes: [
      { code: 'employment', amount: '60000' },
      { code: 'self_employment', amount: '30000' },
    ],
  },
  {
    key: 'multi-source',
    description: 'employment, rental, interest and eligible dividends',
    incomes: [
      { code: 'employment', amount: '70000' },
      { code: 'rental', amount: '15000' },
      { code: 'interest', amount: '2000' },
      { code: 'eligible_dividends', amount: '5000' },
    ],
  },
  {
    key: 'rrsp-contributor',
    description: 'employee with an actual RRSP contribution',
    incomes: [{ code: 'employment', amount: '80000' }],
    registered: [{ type: 'RRSP', contributed: '5000' }],
  },
  {
    key: 'fhsa-contributor',
    description: 'employee with an actual FHSA contribution',
    incomes: [{ code: 'employment', amount: '80000' }],
    registered: [{ type: 'FHSA', contributed: '3000' }],
  },
  {
    key: 'both-registered',
    description: 'employee with RRSP and FHSA contributions',
    incomes: [{ code: 'employment', amount: '80000' }],
    registered: [
      { type: 'RRSP', contributed: '5000' },
      { type: 'FHSA', contributed: '3000' },
    ],
  },
]

/**
 * Post, honouring the backend's admission control.
 *
 * Authentication is deliberately rate-limited per identity AND per source
 * address, and a whole persona suite signing up from one host is exactly the
 * burst that limit exists to slow down. The harness therefore WAITS when it is
 * told to rather than working around the control — a test that disabled the
 * throttle would be testing a system nobody ships.
 */
async function postPaced(
  api: APIRequestContext,
  url: string,
  options: { data: unknown; headers?: Record<string, string> },
): Promise<import('@playwright/test').APIResponse> {
  for (let attempt = 0; attempt < 6; attempt += 1) {
    const response = await api.post(url, options)
    if (response.status() !== 429) return response
    let waitSeconds = 2
    try {
      const body = (await response.json()) as { retry_after_seconds?: number }
      if (typeof body.retry_after_seconds === 'number') {
        waitSeconds = Math.min(body.retry_after_seconds, 20)
      }
    } catch {
      /* no structured body: fall back to the default pause */
    }
    await new Promise((resolve) => setTimeout(resolve, waitSeconds * 1000))
  }
  return api.post(url, options)
}

interface SeededPersona {
  email: string
  analysis: {
    id: string
    estimated_tax: string | null
    taxable_income: string | null
    total_income: string | null
    marginal_rate: string | null
    average_rate: string | null
  }
}

/** Format money exactly as the frontend's own formatter does, so the assertion
 *  compares MEANING (the same value) rather than string style. */
function asDisplayed(value: string | null): string {
  if (value === null) return '—'
  return new Intl.NumberFormat('en-CA', {
    style: 'currency',
    currency: 'CAD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Number(value))
}

async function seedPersona(
  api: APIRequestContext,
  persona: Persona,
): Promise<SeededPersona> {
  const email = `e2e_${persona.key}_${Date.now()}_${Math.floor(Math.random() * 1e6)}@test.ca`

  const registered = await postPaced(api, `${API}/api/v1/auth/register`, {
    data: { email, password: PASSWORD },
  })
  expect(registered.status(), 'register').toBe(201)

  const loggedIn = await postPaced(api, `${API}/api/v1/auth/login`, {
    data: { email, password: PASSWORD },
  })
  expect(loggedIn.status(), 'login').toBe(200)
  const { access_token } = (await loggedIn.json()) as { access_token: string }
  const headers = { authorization: `Bearer ${access_token}` }

  // CONFIRM THE ADDRESS. Registration creates `pending_verification`, and
  // every write below is refused for an account in that state — so this is a
  // precondition of the persona, not a test of verification. It goes through
  // the real endpoint and the real message rather than a shortcut, because a
  // shortcut here would take the whole browser suite off the path customers
  // actually walk.
  //
  // No resend request: REGISTRATION ALREADY SENT THE LINK. Asking for another
  // would be refused by the resend floor, and it would spend a second slice of
  // the auth budget this suite is already paced by.
  const confirmed = await postPaced(api, `${API}/api/v1/auth/verification/confirm`, {
    data: { token: tokenFrom(await waitForMessage(email, 'EMAIL_VERIFICATION')) },
  })
  expect(confirmed.status(), 'confirm verification').toBe(200)

  // CLEAR THE LEGAL GATE. B4 refuses every authenticated write until the
  // account has accepted the current required documents, so this is a
  // precondition of the persona rather than a test of acceptance — the
  // dedicated spec in legal.spec.ts is what proves the flow itself.
  //
  // The required set comes from the SERVER, never a list in this file. A
  // second registry in the test suite is the same mistake B4 forbids the
  // frontend from making, and it would stop covering a document the day one
  // is added.
  // `legalStateAfterVerification` reads the state through a bounded settle: the
  // 200 from the confirm above can arrive before the transaction behind it has
  // committed. That is a recorded server defect, not a slow test — see the
  // helper for the measurement and why it is not repaired there.
  for (const document of (await legalStateAfterVerification(api, headers)).documents) {
    if (!document.acceptance_outstanding) continue
    const accepted = await postPaced(api, `${API}/api/v1/legal/acceptances`, {
      headers,
      data: {
        document_type: document.document_type,
        document_version: document.current_version,
      },
    })
    expect(accepted.status(), `accept ${document.document_type}`).toBe(200)
  }

  const profile = await api.put(`${API}/api/v1/users/me/tax-profile`, {
    headers,
    data: { province_code: 'ON', marital_status: 'single' },
  })
  expect(profile.ok(), 'tax profile').toBeTruthy()

  for (const income of persona.incomes) {
    const created = await api.post(`${API}/api/v1/financials/income`, {
      headers,
      data: { tax_year: TAX_YEAR, income_type_code: income.code, amount: income.amount },
    })
    expect(created.status(), `income ${income.code}`).toBe(201)
  }

  for (const expense of persona.expenses ?? []) {
    const created = await api.post(`${API}/api/v1/financials/expenses`, {
      headers,
      data: {
        tax_year: TAX_YEAR,
        expense_category_code: expense.code,
        amount: expense.amount,
      },
    })
    expect(created.status(), `expense ${expense.code}`).toBe(201)
  }

  for (const account of persona.registered ?? []) {
    const created = await api.post(`${API}/api/v1/financials/registered-accounts`, {
      headers,
      data: {
        tax_year: TAX_YEAR,
        registered_type: account.type,
        contributions_ytd: account.contributed,
      },
    })
    expect(created.status(), `registered ${account.type}`).toBe(201)
  }

  const analysis = await api.post(`${API}/api/v1/analysis`, {
    headers,
    data: { tax_year: TAX_YEAR },
  })
  expect(analysis.status(), 'analysis').toBe(201)

  return { email, analysis: await analysis.json() }
}

/**
 * Drive a real auth form to the product, waiting out the throttle if it fires.
 *
 * Register, login AND refresh all draw on one per-source-address auth budget,
 * so a persona suite working from a single host will legitimately be paced.
 * Retrying after the stated wait is what a real client does; trimming the
 * persona set to dodge the limit would buy a green run by testing less.
 *
 * Sign-in and sign-up share this because they share the budget — the new-user
 * journey was failing for exactly the reason the persona runs were, and one
 * flow tolerating pacing while its twin did not was the bug, not the design.
 *
 * `landsOn` is where success PUTS you, and it is a parameter because the two
 * flows no longer agree: signing in reaches the product, while registering
 * reaches the verification screen — a new account has not confirmed its
 * address and Onyx will not show anybody their tax position until it has.
 * Hard-coding `/app` made the sign-up journey report "did not reach the
 * product" over a screen that was doing exactly the right thing.
 */
async function submitAuthForm(
  page: Page,
  {
    path,
    email,
    button,
    landsOn = /\/app/,
  }: { path: string; email: string; button: RegExp; landsOn?: RegExp },
) {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.goto(path)
    // Filled AND CONFIRMED before submitting. `fill()` writes the DOM value and
    // dispatches `input`; React commits that to state asynchronously, and under
    // parallel load a click can land first — the handler then reads an empty
    // string and the form renders its own validation error, which looks like a
    // broken page and is a race in the test.
    const emailField = page.getByLabel(/email/i)
    const passwordField = page.getByLabel(/password/i)
    await fillField(emailField, email)
    await fillField(passwordField, PASSWORD)
    await page.getByRole('button', { name: button }).click()

    // Race the two real outcomes instead of waiting out the full navigation
    // budget before looking. A refusal renders an alert within a moment, so
    // sitting on a long URL wait spent the test's whole timeout learning
    // something the page had already said. Each branch resolves rather than
    // rejects, so the loser cannot surface as an unhandled rejection after the
    // race has already settled.
    const landed = await Promise.race([
      page
        .waitForURL(landsOn, { timeout: 25_000 })
        .then(() => true)
        .catch(() => false),
      page
        .getByRole('alert')
        .waitFor({ state: 'visible', timeout: 25_000 })
        .then(() => false)
        .catch(() => false),
    ])
    if (landed) return

    // Not in: either we were paced, or something is genuinely wrong.
    const message = (await page.locator('body').innerText()).toLowerCase()
    const throttled = /pacing|too many|try again in|rate limit/.test(message)
    if (!throttled) {
      throw new Error(
        `${path} did not reach ${landsOn}: ${message.slice(0, 300)}`,
      )
    }
    // The pacing message states its own wait ("try again in about N seconds").
    // Honouring what the service asked for is what a real client does, and it
    // is usually far shorter than a fixed guess.
    const stated = /try again in about (\d+) seconds?/.exec(message)?.[1]
    const waitSeconds = Math.min(Number(stated ?? 20) + 2, 45)
    await new Promise((resolve) => setTimeout(resolve, waitSeconds * 1_000))
  }
  throw new Error(`${path} never completed: still paced after three attempts`)
}

function signIn(page: Page, email: string) {
  return submitAuthForm(page, { path: '/sign-in', email, button: /sign in/i })
}

test.describe('value parity: the screen shows the engine figure', () => {
  for (const persona of PERSONAS) {
    test(`${persona.key} — ${persona.description}`, async ({ page, request }, testInfo) => {
      // One viewport: a figure does not change width-dependently, and the
      // mobile project already proves the layout holds. Running the whole
      // persona set twice would only spend the auth budget twice.
      test.skip(testInfo.project.name !== 'desktop', 'desktop project only')
      const seeded = await seedPersona(request, persona)

      // The engine must have produced a figure for this to mean anything.
      expect(seeded.analysis.estimated_tax, 'backend produced no estimated tax').not.toBeNull()

      await signIn(page, seeded.email)

      const expectedTax = asDisplayed(seeded.analysis.estimated_tax)
      const expectedTaxable = asDisplayed(seeded.analysis.taxable_income)

      // The overview headline is the product's loudest claim about someone's
      // money. It must be the engine's number, to the cent.
      await expect(page.getByText(expectedTax, { exact: false }).first()).toBeVisible({
        timeout: 20_000,
      })

      // A hard navigation reloads the SPA, which re-exchanges the refresh token
      // before any protected screen renders. Auto-waiting assertions are
      // required here: reading textContent immediately captures the deliberate
      // "restoring your session" state instead of the page.
      await page.goto('/app/position')
      await expect(page.locator('body'), 'estimated tax on the position screen')
        .toContainText(expectedTax, { timeout: 20_000 })
      if (seeded.analysis.taxable_income !== null) {
        await expect(page.locator('body'), 'taxable income on the position screen')
          .toContainText(expectedTaxable, { timeout: 20_000 })
      }
    })
  }
})

test.describe('decision twin: the product must say which way the money moved', () => {
  test('a contribution that lowers tax is announced as lower', async ({
    page,
    request,
  }, testInfo) => {
    test.skip(testInfo.project.name !== 'desktop', 'desktop project only')

    // This exists because the value-parity journeys asserted that the FIGURE
    // appeared and never that the product read it correctly. It shipped
    // inverted: a reduction was announced as an increase, in red, with an up
    // arrow. Parity of digits is not parity of meaning.
    const seeded = await seedPersona(request, PERSONAS[0]!)
    await signIn(page, seeded.email)

    await page.goto('/app/twin')
    await page.getByLabel(/amount to contribute/i).fill('3000')
    await page.getByRole('button', { name: /model against/i }).click()

    const twin = page.locator('.twin').first()
    const modelledSide = twin.locator('.twin__side--modelled')
    await expect(modelledSide, 'the model did not produce a twin').toBeVisible({
      timeout: 60_000,
    })

    const asNumber = async (locator: ReturnType<typeof page.locator>) => {
      const text = (await locator.locator('.figure').first().innerText()).trim()
      return Number(text.replace(/[^0-9.-]/g, ''))
    }

    const current = await asNumber(twin.locator('.twin__side').first())
    const modelled = await asNumber(modelledSide)
    const word = (await twin.locator('.twin__delta-caption').innerText()).toLowerCase()

    // The test compares the two figures the ENGINE produced and checks the
    // product's own word against them. It computes no tax; it only refuses to
    // let the screen describe a smaller number as a larger one.
    expect(modelled, 'an RRSP deduction should not raise tax').toBeLessThan(current)
    expect(word, `modelled ${modelled} < current ${current}, so this must read lower`).toContain(
      'lower',
    )
    expect(word).not.toContain('higher')
  })
})

test.describe('new user journey', () => {
  test('register, land in the product, and reach the trust surfaces', async ({ page }) => {
    const email = `e2e_new_${Date.now()}@test.ca`

    // Registration lands on the verification screen, not in the product: a new
    // account has not confirmed its address, and Onyx will not show anybody
    // their tax position until it knows the address belongs to them.
    await submitAuthForm(page, {
      path: '/sign-up',
      email,
      button: /create|sign up/i,
      landsOn: /\/verify-email/,
    })
    await expect(page).toHaveURL(/\/verify-email/)
    await expect(page.getByRole('heading', { name: /confirm your email/i })).toBeVisible()

    // Open the link the way a customer does.
    const token = tokenFrom(await waitForMessage(email, 'EMAIL_VERIFICATION'))
    await page.goto(`/verify-email?token=${token}`)

    // CONFIRMING THE ADDRESS DOES NOT OPEN THE PRODUCT — it advances the
    // account to the next gate. B4 asks for the current Terms and Privacy
    // Policy before any tax surface, and the two gates are ordered so the
    // customer only ever sees the one they can act on.
    await expect(page).toHaveURL(/\/legal\/accept/, { timeout: 30_000 })
    await acceptOutstandingLegal(page)
    await expect(page).toHaveURL(/\/app/, { timeout: 30_000 })

    await page.goto('/trust')
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  })

  test('signing in with a wrong password does not reveal whether the account exists', async ({
    page,
    request,
  }) => {
    const seeded = await seedPersona(request, PERSONAS[0]!)

    await page.goto('/sign-in')
    await page.getByLabel(/email/i).fill(seeded.email)
    await page.getByLabel(/password/i).fill('definitely-not-the-password')
    await page.getByRole('button', { name: /sign in/i }).click()
    await expect(page.getByRole('alert')).toBeVisible({ timeout: 20_000 })
    const realAccountMessage = (await page.locator('body').innerText()).toLowerCase()

    await page.goto('/sign-in')
    await page.getByLabel(/email/i).fill(`nobody_${Date.now()}@test.ca`)
    await page.getByLabel(/password/i).fill('definitely-not-the-password')
    await page.getByRole('button', { name: /sign in/i }).click()
    await expect(page.getByRole('alert')).toBeVisible({ timeout: 20_000 })
    const unknownAccountMessage = (await page.locator('body').innerText()).toLowerCase()

    // Neither response may hint that one address is registered and the other
    // is not: that difference is an account-enumeration oracle.
    for (const message of [realAccountMessage, unknownAccountMessage]) {
      expect(message).not.toMatch(/no account|not registered|unknown email|user not found/)
      expect(message).not.toMatch(/incorrect password|wrong password/)
    }
  })
})

test.describe('protected routes', () => {
  test('an anonymous visitor is sent to sign in, not shown someone else data', async ({
    page,
  }) => {
    await page.goto('/app/position')
    await page.waitForURL(/\/sign-in/, { timeout: 20_000 })
    expect(page.url()).toMatch(/\/sign-in/)
  })
})

test.describe('AI explanations stay a renderer, never an authority', () => {
  test('an explanation is produced on request and never claims to decide the figure', async ({
    page,
    request,
  }, testInfo) => {
    test.skip(testInfo.project.name !== 'desktop', 'desktop project only')
    test.setTimeout(120_000)

    // The AI surface had unit coverage of its FRAMING but was never exercised
    // against the real endpoint. A trust surface that has never round-tripped
    // is not a verified trust surface.
    const seeded = await seedPersona(request, PERSONAS[0]!)
    await signIn(page, seeded.email)

    await page.goto('/app/twin')
    await page.getByLabel(/amount to contribute/i).fill('3000')
    await page.getByRole('button', { name: /model against/i }).click()
    await expect(page.locator('.twin__side--modelled')).toBeVisible({ timeout: 60_000 })

    // Before asking, the product states the posture rather than pre-generating.
    await expect(page.getByText(/does not decide any amount/i)).toBeVisible()

    await page.getByRole('button', { name: /explain this model/i }).click()

    const panel = page.locator('section[aria-labelledby="twin-explain-heading"]')
    // Either an explanation lands or the surface says plainly that it could
    // not produce one. Both are acceptable; silence is not.
    await expect
      .poll(async () => (await panel.innerText()).toLowerCase(), { timeout: 60_000 })
      .not.toMatch(/^\s*why the figure moved\s*$/)

    const text = (await panel.innerText()).toLowerCase()

    // Whatever came back, the authority separation must hold on screen.
    expect(text, 'the AI surface must stay labelled').toMatch(/onyx|explanation|written|plain/)
    expect(text, 'AI must never be presented as deciding tax').not.toMatch(
      /\bai (decided|determined|calculated) (your|the) (tax|amount|figure)\b/,
    )
    expect(text, 'no model or vendor may be named to the customer').not.toMatch(
      /gpt|claude|llama|openai|anthropic|gemini|model version/,
    )
    // Internal machinery never reaches a customer: not a hash, not a mode.
    expect(text).not.toMatch(/renderer_mode|fallback|prompt|token|[0-9a-f]{32}/)
  })
})
