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
import { seedUsableAccount } from './account'
import { type } from './form'

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
  //
  // PAST BOTH GATES, and every seeding call below checked. This sweep used to
  // register and go straight to signing in: the account was never confirmed,
  // so all four writes were refused with a 403 nobody asserted on, and axe
  // scanned `/verify-email` eight times under eight product-screen names.
  const { headers } = await seedUsableAccount(request, email, PASSWORD)

  const profile = await request.put(`${API}/api/v1/users/me/tax-profile`, {
    headers,
    data: { province_code: 'ON', marital_status: 'single' },
  })
  expect(profile.status(), 'tax profile').toBe(200)
  const income = await request.post(`${API}/api/v1/financials/income`, {
    headers,
    data: { tax_year: TAX_YEAR, income_type_code: 'employment', amount: '90000' },
  })
  expect(income.status(), 'income').toBe(201)
  const expense = await request.post(`${API}/api/v1/financials/expenses`, {
    headers,
    data: { tax_year: TAX_YEAR, expense_category_code: 'donation', amount: '5000' },
  })
  expect(expense.status(), 'expense').toBe(201)
  const analysis = await request.post(`${API}/api/v1/analysis`, {
    headers,
    data: { tax_year: TAX_YEAR },
  })
  expect(analysis.status(), 'analysis').toBe(201)

  await page.goto('/sign-in')
  await type(page, /email/i, email)
  await type(page, /password/i, PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
  await page.waitForURL(/\/app/, { timeout: 30_000 })

  const failures: string[] = []

  for (const screen of SCREENS) {
    await page.goto(screen.path)
    // STILL ON THE SCREEN WE ASKED FOR. A gate that bounced the browser
    // elsewhere would leave axe scanning a small public page and reporting it
    // clean under a product screen's name — which is how this sweep spent two
    // entries passing over nothing at all.
    await expect(page, `${screen.name} bounced away from ${screen.path}`).toHaveURL(
      new RegExp(`${screen.path}$`),
      { timeout: 30_000 },
    )
    // Wait for the route's own heading: scanning the Suspense fallback would
    // report a clean sheet for a page that never rendered.
    await page.getByRole('heading', { level: 1 }).first().waitFor({ state: 'visible' })

    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
      .analyze()

    for (const violation of results.violations) {
      // Name the elements, not just the rule. A failure that says "contrast is
      // wrong somewhere on eight screens" costs an investigation; one that
      // prints the selector and the measured ratio is a fix.
      const nodes = violation.nodes
        .map((node) => {
          const summary = (node.failureSummary ?? '').replace(/\s+/g, ' ').trim()
          return `      ${node.target.join(' ')} — ${summary}`
        })
        .join('\n')
      failures.push(
        `${screen.name} (${screen.path}) — ${violation.id}: ${violation.help} [${violation.nodes.length} node(s)]\n${nodes}`,
      )
    }
  }

  expect(failures, failures.join('\n')).toEqual([])
})
