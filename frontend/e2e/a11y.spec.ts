/* =========================================================================
   ACCESSIBILITY — the authenticated product surfaces
   =========================================================================
   The public specs scan the marketing and legal pages. This one gets inside
   the product, where the complex surfaces live: the assurance ledger, the
   position breakdown, the Decision Twin bench and the change timeline.

   One account, one sign-in, several screens. Authentication draws on a shared
   per-address budget, so scanning each screen in its own test would spend the
   budget on repeated logins rather than on coverage.
   ========================================================================= */
import { AxeBuilder } from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

const API = process.env.ONYX_E2E_API ?? 'http://127.0.0.1:8099'
const PASSWORD = 'supersecret1'
const TAX_YEAR = 2025

const SCREENS = [
  { path: '/app', name: 'assurance overview' },
  { path: '/app/position', name: 'tax position' },
  { path: '/app/opportunities', name: 'opportunities' },
  { path: '/app/twin', name: 'decision twin' },
  { path: '/app/evidence', name: 'evidence' },
  { path: '/app/changes', name: 'what changed' },
  { path: '/app/settings', name: 'settings' },
  { path: '/app/onboarding', name: 'onboarding' },
]

test('authenticated surfaces have no detectable accessibility violations', async ({
  page,
  request,
}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one viewport is enough for rule violations')
  test.setTimeout(180_000)

  const email = `e2e_a11y_${Date.now()}@test.ca`

  // Seed a real account with a real analysis, so the screens under test are
  // rendering genuine engine output rather than empty states.
  const registered = await request.post(`${API}/api/v1/auth/register`, {
    data: { email, password: PASSWORD },
  })
  expect(registered.status()).toBe(201)
  const loggedIn = await request.post(`${API}/api/v1/auth/login`, {
    data: { email, password: PASSWORD },
  })
  expect(loggedIn.status()).toBe(200)
  const { access_token } = (await loggedIn.json()) as { access_token: string }
  const headers = { authorization: `Bearer ${access_token}` }

  await request.put(`${API}/api/v1/users/me/tax-profile`, {
    headers,
    data: { province_code: 'ON', marital_status: 'single' },
  })
  await request.post(`${API}/api/v1/financials/income`, {
    headers,
    data: { tax_year: TAX_YEAR, income_type_code: 'employment', amount: '90000' },
  })
  await request.post(`${API}/api/v1/financials/expenses`, {
    headers,
    data: { tax_year: TAX_YEAR, expense_category_code: 'donation', amount: '5000' },
  })
  await request.post(`${API}/api/v1/analysis`, {
    headers,
    data: { tax_year: TAX_YEAR },
  })

  await page.goto('/sign-in')
  await page.getByLabel(/email/i).fill(email)
  await page.getByLabel(/password/i).fill(PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
  await page.waitForURL(/\/app/, { timeout: 30_000 })

  const failures: string[] = []

  for (const screen of SCREENS) {
    await page.goto(screen.path)
    // Wait for the route's own heading: scanning the Suspense fallback would
    // report a clean sheet for a page that never rendered.
    await page.getByRole('heading', { level: 1 }).first().waitFor({ state: 'visible' })

    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
      .analyze()

    for (const violation of results.violations) {
      failures.push(
        `${screen.name} (${screen.path}) — ${violation.id}: ${violation.help} [${violation.nodes.length} node(s)]`,
      )
    }
  }

  expect(failures, failures.join('\n')).toEqual([])
})
