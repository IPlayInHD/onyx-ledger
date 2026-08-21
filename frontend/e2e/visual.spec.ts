/* =========================================================================
   VISUAL CAPTURE — for human design review, not for assertion
   =========================================================================
   This does not test anything. It drives a real account through the real
   product and writes full-page images so a person (or a model) can LOOK at
   the result: hierarchy, density, rhythm, whether the provenance system reads
   at a glance, and whether the thing has any character of its own.

   Screenshot DIFFING is deliberately not done here. A pixel baseline on a
   surface whose figures come from a live engine would fail on a legitimate
   change to someone's tax position, and a test that cries wolf gets muted.

   Opt-in: ONYX_VISUAL=1. Otherwise a routine suite run would spend two
   minutes producing artefacts nobody asked for.
   ========================================================================= */
import { expect, test } from '@playwright/test'

const API = process.env.ONYX_E2E_API ?? 'http://127.0.0.1:8099'
const PASSWORD = 'supersecret1'
const TAX_YEAR = 2025
const OUT = 'visual'

/** Enough facts that every screen has something real to render. An empty
 *  product photographs beautifully and tells you nothing. */
async function seed(request: import('@playwright/test').APIRequestContext) {
  const email = `e2e_visual_${Date.now()}@test.ca`
  const registered = await request.post(`${API}/api/v1/auth/register`, {
    data: { email, password: PASSWORD },
  })
  expect(registered.status()).toBe(201)
  const loggedIn = await request.post(`${API}/api/v1/auth/login`, {
    data: { email, password: PASSWORD },
  })
  const { access_token } = (await loggedIn.json()) as { access_token: string }
  const headers = { authorization: `Bearer ${access_token}` }

  await request.put(`${API}/api/v1/users/me/tax-profile`, {
    headers,
    data: { province_code: 'ON', marital_status: 'single' },
  })
  await request.post(`${API}/api/v1/financials/income`, {
    headers,
    data: { tax_year: TAX_YEAR, income_type_code: 'employment', amount: '96000' },
  })
  await request.post(`${API}/api/v1/financials/income`, {
    headers,
    data: { tax_year: TAX_YEAR, income_type_code: 'interest', amount: '2400' },
  })
  await request.post(`${API}/api/v1/financials/expenses`, {
    headers,
    data: { tax_year: TAX_YEAR, expense_category_code: 'donation', amount: '3000' },
  })
  // Medical expenses are, on the currently published rule set, the one input
  // that makes the optimizer emit an opportunity at all — a BLOCKED one, under
  // a governed exclusion. Seeding it is what lets the opportunity surface be
  // reviewed populated instead of empty.
  await request.post(`${API}/api/v1/financials/expenses`, {
    headers,
    data: { tax_year: TAX_YEAR, expense_category_code: 'medical', amount: '6000' },
  })
  await request.post(`${API}/api/v1/financials/registered-accounts`, {
    headers,
    data: { tax_year: TAX_YEAR, registered_type: 'rrsp', contributions_ytd: '4000' },
  })
  // An analysis alone leaves every opportunity family reading "not established
  // yet", so the flagship screens would photograph as empty states. Running the
  // optimization is what gives Opportunities, Evidence and the assurance ledger
  // real engine output to render.
  const analysis = await request.post(`${API}/api/v1/analysis`, {
    headers,
    data: { tax_year: TAX_YEAR },
  })
  expect(analysis.status()).toBe(201)
  const { id: analysisId } = (await analysis.json()) as { id: string }

  const optimization = await request.post(`${API}/api/v1/ioe/optimizations`, {
    headers,
    data: { analysis_id: analysisId },
  })
  // Recorded rather than asserted: if the engine declines to optimize this
  // profile, the capture is still worth having — it just photographs the
  // honest empty state, which is itself a design surface worth reviewing.
  console.log(`optimization run: ${optimization.status()}`)
  return email
}

const SCREENS = [
  ['landing', '/'],
  ['overview', '/app'],
  ['position', '/app/position'],
  ['opportunities', '/app/opportunities'],
  ['twin', '/app/twin'],
  ['before-you-act', '/app/before-you-act'],
  ['evidence', '/app/evidence'],
  ['changes', '/app/changes'],
  ['onboarding', '/app/onboarding'],
  ['settings', '/app/settings'],
  ['trust', '/trust'],
  ['legal', '/legal/terms'],
] as const

test('capture the product for design review', async ({ page, request }, testInfo) => {
  test.skip(!process.env.ONYX_VISUAL, 'set ONYX_VISUAL=1 to capture')
  test.setTimeout(300_000)

  const theme = process.env.ONYX_VISUAL_THEME ?? 'light'
  const email = await seed(request)

  // Seed the stored preference only. The application applies it at startup, so
  // seeding storage exercises the real path rather than faking the result —
  // which is how a capture run stays evidence about the product.
  //
  // This script runs BEFORE the parser creates <html>, so `documentElement` is
  // null here. An earlier version assigned to it directly, threw, and silently
  // produced twelve light screenshots into dark-named files.
  await page.addInitScript(
    ([key, value]) => {
      try {
        if (value === 'system') window.localStorage.removeItem(key as string)
        else window.localStorage.setItem(key as string, value as string)
      } catch {
        /* ignore */
      }
    },
    ['onyx.theme', theme],
  )

  await page.goto('/sign-in')
  await page.getByLabel(/email/i).fill(email)
  await page.getByLabel(/password/i).fill(PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
  await page.waitForURL(/\/app/, { timeout: 30_000 })

  // Say which theme actually took effect. A capture run that silently produced
  // twelve light screenshots into dark-*.png would be worse than no capture,
  // because it looks like evidence.
  const applied = await page.evaluate(() => ({
    attribute: document.documentElement.dataset.theme ?? 'unset',
    stored: (() => {
      try {
        return window.localStorage.getItem('onyx.theme') ?? 'unset'
      } catch {
        return 'unreadable'
      }
    })(),
    background: getComputedStyle(document.body).backgroundColor,
  }))
  console.log(`theme requested=${theme} ${JSON.stringify(applied)}`)

  const project = testInfo.project.name
  const shoot = async (name: string) => {
    // Let figure transitions settle so the capture is the resting state, not
    // a frame mid-animation.
    await page.waitForTimeout(600)
    await page.screenshot({
      path: `${OUT}/${theme}-${project}-${name}.png`,
      fullPage: true,
    })
  }

  for (const [name, path] of SCREENS) {
    await page.goto(path)
    await page.getByRole('heading', { level: 1 }).first().waitFor({ state: 'visible' })
    await shoot(name)
  }

  // The opportunity RECORD is where the full treatment lives — what it is, why
  // it may apply, what it costs, what it needs, what it assumes. The queue is
  // deliberately terse, so reviewing only the queue would review the wrong
  // surface.
  await page.goto('/app/opportunities')
  // Wait for the route to mount: the screens are lazy, so asking a freshly
  // navigated page what is visible answers about the Suspense fallback.
  await page.getByRole('heading', { level: 1 }).first().waitFor({ state: 'visible' })
  const record = page.getByRole('link', { name: /open the record/i }).first()
  if (await record.isVisible().catch(() => false)) {
    await record.click()
    await page.getByRole('heading', { level: 1 }).first().waitFor({ state: 'visible' })
    await shoot('opportunity-record')
  }

  // The Decision Twin's whole point is the comparison, and an unmodelled bench
  // photographs as an empty panel. Drive it to an actual modelled position so
  // the flagship surface can be reviewed as customers will meet it.
  await page.goto('/app/twin')
  await page.getByRole('heading', { level: 1 }).first().waitFor({ state: 'visible' })
  await page.getByLabel(/amount to contribute/i).fill('8000')
  await page.getByRole('button', { name: /model against/i }).click()
  await page
    .getByText(/modelled position|modelled state|refus/i)
    .first()
    .waitFor({ state: 'visible', timeout: 60_000 })
    .catch(() => {
      /* Captured either way: a refusal is a designed surface too. */
    })
  await shoot('twin-modelled')

  // The AI explanation is generated on request, so a capture that never asks
  // photographs only the invitation to ask.
  const explain = page.getByRole('button', { name: /explain this model/i })
  if (await explain.isVisible().catch(() => false)) {
    await explain.click()
    await page
      .locator('section[aria-labelledby="twin-explain-heading"] p')
      .nth(1)
      .waitFor({ state: 'visible', timeout: 60_000 })
      .catch(() => {})
    await shoot('twin-explained')
  }

  // A viewport-only frame at rest. Full-page capture stitches, and a sticky
  // navigation bar lands wherever it was at the final scroll offset — which
  // reads in the stitched image as a nav bar dropped into the middle of the
  // page. This frame is what the customer actually sees, and is how that
  // artefact is told apart from a genuine layout fault.
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.waitForTimeout(300)
  await page.screenshot({
    path: `${OUT}/${theme}-${project}-twin-viewport.png`,
    fullPage: false,
  })
})
