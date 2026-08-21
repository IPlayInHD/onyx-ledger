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

  const registered = await api.post(`${API}/api/v1/auth/register`, {
    data: { email, password: PASSWORD },
  })
  expect(registered.status(), 'register').toBe(201)

  const loggedIn = await api.post(`${API}/api/v1/auth/login`, {
    data: { email, password: PASSWORD },
  })
  expect(loggedIn.status(), 'login').toBe(200)
  const { access_token } = (await loggedIn.json()) as { access_token: string }
  const headers = { authorization: `Bearer ${access_token}` }

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

async function signIn(page: Page, email: string) {
  await page.goto('/sign-in')
  await page.getByLabel(/email/i).fill(email)
  await page.getByLabel(/password/i).fill(PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
  await page.waitForURL(/\/app/, { timeout: 20_000 })
}

test.describe('value parity: the screen shows the engine figure', () => {
  for (const persona of PERSONAS) {
    test(`${persona.key} — ${persona.description}`, async ({ page, request }) => {
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

      await page.goto('/app/position')
      const positionBody = (await page.textContent('body')) ?? ''
      expect(positionBody, 'estimated tax on the position screen').toContain(expectedTax)
      if (seeded.analysis.taxable_income !== null) {
        expect(positionBody, 'taxable income on the position screen').toContain(expectedTaxable)
      }
    })
  }
})

test.describe('new user journey', () => {
  test('register, land in the product, and reach the trust surfaces', async ({ page }) => {
    const email = `e2e_new_${Date.now()}@test.ca`

    await page.goto('/sign-up')
    await page.getByLabel(/email/i).fill(email)
    await page.getByLabel(/password/i).fill(PASSWORD)
    await page.getByRole('button', { name: /create|sign up/i }).click()

    // Registration lands in onboarding, because a new account has no facts yet
    // and an empty position would be a meaningless first impression.
    await page.waitForURL(/\/app/, { timeout: 20_000 })
    expect(page.url()).toMatch(/\/app/)

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
    const realAccountMessage = await page
      .locator('body')
      .innerText()
      .then((t) => t.toLowerCase())

    await page.goto('/sign-in')
    await page.getByLabel(/email/i).fill(`nobody_${Date.now()}@test.ca`)
    await page.getByLabel(/password/i).fill('definitely-not-the-password')
    await page.getByRole('button', { name: /sign in/i }).click()
    const unknownAccountMessage = await page
      .locator('body')
      .innerText()
      .then((t) => t.toLowerCase())

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
