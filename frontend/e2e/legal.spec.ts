/* =========================================================================
   LEGAL ACCEPTANCE  —  in a real browser
   =========================================================================
   The record this flow writes is meant to prove a person agreed to something.
   These tests are mostly about the ways the SCREEN could make that record a
   lie: a box already ticked, a button live before the box, a version on the
   page that is not the version being sent, or tax data visible to somebody
   the backend has refused.
   ========================================================================= */
import { AxeBuilder } from '@axe-core/playwright'
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { type } from './form'
import { acceptOutstandingLegal, waitForLegalScreen } from './legal'
import { tokenFrom, waitForMessage } from './mailbox'

const API = process.env.ONYX_E2E_API ?? 'http://127.0.0.1:8099'
const PASSWORD = 'supersecret1'

function freshEmail(tag: string): string {
  return `e2e_${tag}_${Date.now()}_${Math.floor(Math.random() * 1e6)}@test.ca`
}

/** A verified account that has accepted nothing, signed in in the browser. */
async function signedInPendingLegal(
  page: Page,
  api: APIRequestContext,
): Promise<string> {
  const email = freshEmail('legal')
  const created = await api.post(`${API}/api/v1/auth/register`, {
    data: { email, password: PASSWORD },
  })
  expect(created.status(), 'register').toBe(201)

  // Redeem the link registration already sent.
  const token = tokenFrom(await waitForMessage(email, 'EMAIL_VERIFICATION'))
  const confirmed = await api.post(`${API}/api/v1/auth/verification/confirm`, {
    data: { token },
  })
  expect(confirmed.status(), 'confirm').toBe(200)

  await page.goto('/sign-in')
  await type(page, /email/i, email)
  await type(page, /password/i, PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
  return email
}

test.describe('legal acceptance', () => {
  test('a verified account is sent to the terms, not into the product', async ({
    page,
    request,
  }) => {
    await signedInPendingLegal(page, request)

    await expect(page).toHaveURL(/\/legal\/accept/, { timeout: 30_000 })
    await waitForLegalScreen(page)
    const body = (await page.locator('main').innerText()).toLowerCase()

    // The customer is told WHY, and it is not phrased as a failure.
    expect(body).toMatch(/terms of service/)
    expect(body).toMatch(/privacy policy/)
    expect(body).not.toMatch(/forbidden|access denied|error 403/)
  })

  test('nothing is pre-ticked and no button is live before its box', async ({
    page,
    request,
  }) => {
    await signedInPendingLegal(page, request)
    await expect(page).toHaveURL(/\/legal\/accept/, { timeout: 30_000 })
    await waitForLegalScreen(page)

    // A box the product ticked is the product asserting agreement on the
    // customer's behalf — the exact thing the durable record exists to
    // disprove.
    const boxes = page.getByRole('checkbox')
    const count = await boxes.count()
    expect(count).toBeGreaterThan(0)
    for (let i = 0; i < count; i += 1) {
      await expect(boxes.nth(i)).not.toBeChecked()
    }

    const buttons = page.getByRole('button', { name: /^Accept / })
    const buttonCount = await buttons.count()
    expect(buttonCount).toBe(count)
    for (let i = 0; i < buttonCount; i += 1) {
      await expect(buttons.nth(i)).toBeDisabled()
    }
  })

  test('the version on the page is the version that gets recorded', async ({
    page,
    request,
  }) => {
    const email = await signedInPendingLegal(page, request)
    await expect(page).toHaveURL(/\/legal\/accept/, { timeout: 30_000 })

    // WAIT FOR THE DOCUMENTS, not just the URL. The screen renders "Loading
    // the current documents…" first and reading its text at that moment
    // compares the version against a spinner — which fails for a reason that
    // has nothing to do with what this test is about.
    await expect(page.getByRole('button', { name: /^Accept / }).first()).toBeVisible({
      timeout: 30_000,
    })
    const shown = await page.locator('main').innerText()

    // What the SERVER says is current, asked independently of the page. A
    // version rendered from anything other than this is the defect: a durable
    // record saying somebody accepted 2.0 while their browser showed 1.9.
    const signedIn = await request.post(`${API}/api/v1/auth/login`, {
      data: { email, password: PASSWORD },
    })
    expect(signedIn.status(), 'login').toBe(200)
    const authed = await request.get(`${API}/api/v1/legal/state`, {
      headers: { authorization: `Bearer ${(await signedIn.json()).access_token}` },
    })
    expect(authed.status(), 'legal state').toBe(200)

    for (const document of (await authed.json()).documents) {
      if (!document.acceptance_outstanding) continue
      // The page must display the same version string it is about to send.
      expect(shown, `${document.document_type} version not shown`).toContain(
        document.current_version,
      )
    }
  })

  test('accepting opens the product, and the record survives a reload', async ({
    page,
    request,
  }) => {
    await signedInPendingLegal(page, request)
    await expect(page).toHaveURL(/\/legal\/accept/, { timeout: 30_000 })

    await acceptOutstandingLegal(page)
    await expect(page).toHaveURL(/\/app/, { timeout: 30_000 })

    // Durable, not a client flag: a full reload re-reads the session and the
    // gate from the server and must still let the customer in.
    await page.reload()
    await expect(page).toHaveURL(/\/app/, { timeout: 30_000 })
  })

  test('no tax surface is reachable while acceptance is outstanding', async ({
    page,
    request,
  }) => {
    await signedInPendingLegal(page, request)
    await expect(page).toHaveURL(/\/legal\/accept/, { timeout: 30_000 })

    // Every product route bounces back, rather than rendering an empty shell
    // that looks like the customer has no data.
    for (const route of ['/app', '/app/position', '/app/evidence', '/app/settings']) {
      await page.goto(route)
      await expect(page, `${route} was reachable`).toHaveURL(/\/legal\/accept/, {
        timeout: 30_000,
      })
    }
  })

  test('the documents are readable before they are accepted', async ({
    page,
    request,
  }) => {
    await signedInPendingLegal(page, request)
    await expect(page).toHaveURL(/\/legal\/accept/, { timeout: 30_000 })

    // Being asked to agree to something you cannot read is the failure this
    // prevents. The public document routes stay reachable while gated.
    await page.goto('/legal/terms')
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
    expect((await page.locator('body').innerText()).toLowerCase()).toContain('draft')
  })

  test('has no detectable accessibility violations', async ({ page, request }) => {
    await signedInPendingLegal(page, request)
    await expect(page).toHaveURL(/\/legal\/accept/, { timeout: 30_000 })
    await waitForLegalScreen(page)

    // Runs on BOTH viewport projects, so the mobile layout is scanned too.
    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
      .analyze()
    expect(
      results.violations.map((v) => `${v.id}: ${v.description}`),
      'axe violations',
    ).toEqual([])
  })

  test('reflows at 400% zoom without a horizontal scrollbar', async ({ page, request }) => {
    await signedInPendingLegal(page, request)
    await expect(page).toHaveURL(/\/legal\/accept/, { timeout: 30_000 })
    await waitForLegalScreen(page)

    // WCAG 2.2 §1.4.10 reflow: 320 CSS pixels wide is what 1280px at 400%
    // zoom becomes. A customer who has to scroll sideways to find the button
    // that unblocks their account is blocked by the screen meant to unblock
    // them.
    await page.setViewportSize({ width: 320, height: 512 })
    await expect(page.getByRole('button', { name: /^Accept / }).first()).toBeVisible()
    const overflows = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    )
    expect(overflows, 'the acceptance screen scrolls horizontally at 320px').toBe(false)
  })

  test('a failure to load says so, and offers a way back', async ({ page, request }) => {
    await signedInPendingLegal(page, request)
    await expect(page).toHaveURL(/\/legal\/accept/, { timeout: 30_000 })
    await waitForLegalScreen(page)

    // THE ERROR STATE IS PART OF THE GATE. A customer who cannot load the
    // documents cannot use the product, so a blank screen here is an account
    // they cannot recover on their own. Refuse the read and require that the
    // page says so out loud — and that retrying actually works.
    let refuse = true
    await page.route('**/api/v1/legal/state', async (route) => {
      if (refuse) await route.fulfill({ status: 503, body: '{"detail":"unavailable"}' })
      else await route.continue()
    })
    await page.reload()

    const alert = page.getByRole('alert')
    await expect(alert).toBeVisible({ timeout: 30_000 })
    // Never a claim that anything was accepted, and never a silent empty page.
    await expect(page.getByRole('button', { name: /^Accept / })).toHaveCount(0)

    refuse = false
    await page.getByRole('button', { name: /try again/i }).click()
    await expect(page.getByRole('button', { name: /^Accept / }).first()).toBeVisible({
      timeout: 30_000,
    })
  })

  test('the acceptance screen is not a keyboard trap', async ({ page, request }) => {
    await signedInPendingLegal(page, request)
    await expect(page).toHaveURL(/\/legal\/accept/, { timeout: 30_000 })

    // A route, not a modal — so focus can leave it. Tabbing repeatedly must
    // reach something outside the acceptance controls.
    const reached = new Set<string>()
    for (let i = 0; i < 30; i += 1) {
      await page.keyboard.press('Tab')
      reached.add(
        await page.evaluate(() => document.activeElement?.tagName ?? 'NONE'),
      )
    }
    expect(reached.has('A'), 'no link was reachable by keyboard').toBeTruthy()
  })
})
